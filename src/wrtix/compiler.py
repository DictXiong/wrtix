"""Compile public Nix fleet descriptions to one owned OpenWrt resource plan."""
from __future__ import annotations

import copy
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import signal

from secret_refs import SecretError, SecretResolver, reference


class CompileError(ValueError):
    """Diagnostics contain resource names, never configuration values."""


NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+-]*$")
SECTION = re.compile(r"^[A-Za-z0-9_]+$")
SENTINEL_PASSWORD = "openwrt-preview-password"
SENTINEL_HASH = "$6$preview$" + "A" * 86


def contract_path():
    return Path(os.environ.get("WRTIX_AGENT_DATA", Path(__file__).resolve().parents[2] / "agent")) / "contract.json"


def load_contract():
    try:
        data = contract_path().read_bytes()
        value = json.loads(data)
        if value.get("profile") != "wrtix-v2":
            raise ValueError()
        return value, digest(data)
    except (OSError, ValueError):
        raise CompileError("wrtix protocol contract is unavailable") from None


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate_path(path):
    if not isinstance(path, str) or not path.startswith("/") or path == "/":
        raise CompileError("managed file requires an absolute path")
    rel = path[1:]
    if any(not part or part in (".", "..") for part in rel.split("/")) or not re.fullmatch(r"[A-Za-z0-9_./+-]+", rel):
        raise CompileError("unsafe managed file path")
    blocked = {"etc/shadow", "etc/passwd", "etc/group", "etc/gshadow", "etc/resolv.conf",
               "etc/ssh", "etc/rc.d", "etc/wrtix"}
    if (rel in blocked or rel.startswith(("etc/dropbear/dropbear_", "etc/ssh/", "etc/wrtix/", "etc/rc.d/"))
            or rel == "etc/init.d/wrtix-agent"):
        raise CompileError("file destination belongs to a protected system resource")
    if rel.startswith("etc/init.d/") and not rel.startswith("etc/init.d/wrtix-managed-"):
        raise CompileError("custom init scripts require the wrtix-managed- namespace")
    if not (rel.startswith("etc/") or rel.startswith("usr/libexec/wrtix-managed/")):
        raise CompileError("managed file destination is outside supported configuration roots")
    return rel


def scalar(value):
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and not any(ord(c) < 32 or ord(c) == 127 for c in value):
        return value
    raise CompileError("UCI value must be a scalar string, integer or boolean")


def quote(value):
    return "'" + scalar(value).replace("'", "'\\''") + "'"


def parse_uci(data):
    sections, current = {}, None
    counts = {}
    for line in data.decode().splitlines():
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            raise CompileError("generated UCI could not be parsed") from None
        if not tokens:
            continue
        if tokens[0] == "config" and len(tokens) in (2, 3):
            typ = tokens[1]
            anonymous = len(tokens) == 2
            count = counts.get(typ, 0)
            counts[typ] = count + 1
            name = f"__anonymous_{typ}_{count}" if anonymous else tokens[2]
            if name in sections:
                raise CompileError("duplicate generated UCI section")
            current = {"type": typ, "anonymous": anonymous, "order": len(sections), "options": {}}
            sections[name] = current
        elif tokens[0] in ("option", "list") and len(tokens) == 3 and current is not None:
            key, value = tokens[1:]
            if tokens[0] == "list":
                if key in current["options"] and not isinstance(current["options"][key], list):
                    raise CompileError("mixed scalar and list UCI option")
                current["options"].setdefault(key, []).append(value)
            else:
                current["options"][key] = value
        else:
            raise CompileError("unsupported generated UCI syntax")
    return sections


def serialize_uci(sections):
    rows = []
    for name, section in sorted(sections.items(), key=lambda pair: (pair[1].get("order", 0), pair[0])):
        if not SECTION.fullmatch(name) or not NAME.fullmatch(section["type"]):
            raise CompileError("invalid UCI section name or type")
        rows.append("config " + section["type"] + ("" if section.get("anonymous") else " " + quote(name)))
        for key, value in section["options"].items():
            if not SECTION.fullmatch(key):
                raise CompileError("invalid UCI option name")
            if isinstance(value, list):
                rows.extend("\tlist " + key + " " + quote(item) for item in value)
            else:
                rows.append("\toption " + key + " " + quote(value))
        rows.append("")
    return ("\n".join(rows) + "\n").encode()


def placeholder(value):
    if reference(value) is not None:
        return SENTINEL_PASSWORD
    if isinstance(value, dict):
        return {key: placeholder(child) for key, child in value.items()}
    if isinstance(value, list):
        return [placeholder(child) for child in value]
    return value



def protected(ownership, package, option):
    return (package in ownership.get("protectedPackages", [])
            or option in ownership.get("protectedOptions", {}).get(package, []))


def merge_uci(files, node, ownership):
    resources = {}
    for path, data in files.items():
        if path.startswith("etc/config/"):
            resources[path[11:]] = parse_uci(data)
    for package, sections in node.get("uci", {}).items():
        if not NAME.fullmatch(package):
            raise CompileError("invalid UCI package name")
        target = resources.setdefault(package, {})
        for name, spec in sections.items():
            if not SECTION.fullmatch(name) or not NAME.fullmatch(spec["type"]):
                raise CompileError("invalid UCI section identifier")
            if name in target:
                current = target[name]
                if spec["type"] != current["type"] or spec.get("anonymous", False) != current["anonymous"]:
                    raise CompileError("UCI section type conflicts with its owner")
                for key, value in spec.get("options", {}).items():
                    normalized = [scalar(item) for item in value] if isinstance(value, list) else scalar(value)
                    if key in current["options"] and current["options"][key] != normalized:
                        raise CompileError(f"UCI field conflict: {package}.{name}.{key}; use its profile option or explicit override")
                    if key not in current["options"] and protected(ownership, package, key):
                        raise CompileError("producer-owned resource cannot be extended through raw configuration")
                    current["options"][key] = normalized
            else:
                target[name] = copy.deepcopy(spec)
                target[name].setdefault("order", len(target) + 1000)
    for package, sections in node.get("overrides", {}).get("uci", {}).items():
        for name, spec in sections.items():
            if package not in resources or name not in resources[package]:
                raise CompileError("override names a missing UCI section")
            options = resources[package][name]["options"]
            for key, value in spec.get("options", {}).items():
                if key not in options:
                    raise CompileError("override names a missing UCI option")
                if protected(ownership, package, key):
                    raise CompileError("override would violate resource ownership")
                options[key] = value
            for key in spec.get("removeOptions", []):
                if key not in options or protected(ownership, package, key):
                    raise CompileError("cannot remove missing or protected UCI option")
                del options[key]
    for package, sections in resources.items():
        files["etc/config/" + package] = serialize_uci(sections)


def prepare_resources(node, reserved_services):
    files, modes = {}, {}
    ownership = node.get("ownership", {})
    users = node.get("users", {})
    if not isinstance(users, dict) or set(users) - {"root"} or not isinstance(users.get("root", {}), dict) or set(users.get("root", {})) - {"passwordHash", "authorizedKeys"}:
        raise CompileError("unsupported user resource fields")
    merge_uci(files, node, ownership)
    modes = {path: "600" for path in files}
    for path, spec in node.get("files", {}).items():
        if not isinstance(spec, dict) or set(spec) - {"text", "source", "value", "format", "mode"}:
            raise CompileError("unsupported file resource fields")
        rel = validate_path(path)
        if rel.startswith("etc/config/"):
            raise CompileError("UCI configuration must use the structured UCI interface")
        if rel in files:
            raise CompileError("file destination has multiple owners")
        choices = [key for key in ("text", "source", "value") if spec.get(key) is not None]
        if len(choices) != 1:
            raise CompileError("file requires exactly one of text, source or value")
        if choices[0] == "source":
            try:
                data = Path(spec["source"]).read_bytes()
            except OSError:
                raise CompileError("file source is unavailable") from None
        elif choices[0] == "text":
            data = spec["text"].encode()
        elif spec.get("format", "text") == "json":
            data = (json.dumps(spec["value"], ensure_ascii=False, indent=2) + "\n").encode()
        elif spec.get("format") == "yaml":
            import yaml
            data = yaml.safe_dump(spec["value"], allow_unicode=True, sort_keys=False).encode()
        elif isinstance(spec["value"], str):
            data = spec["value"].encode()
        else:
            raise CompileError("text file value must be a string")
        mode = str(spec.get("mode", "0644")).lstrip("0")
        if mode not in ("600", "644", "700", "755"):
            raise CompileError("unsupported managed file mode")
        files[rel], modes[rel] = data, mode
    services = {}
    for service, spec in node.get("services", {}).items():
        if not isinstance(spec, dict) or set(spec) - {"enable", "running", "oneshot", "action", "reloadOn"}:
            raise CompileError("unsupported service resource fields")
        if not NAME.fullmatch(service) or len(service) > 64:
            raise CompileError("invalid service name")
        if service in reserved_services:
            raise CompileError("the independent rollback guard is reserved")
        enabled = spec.get("enable", True)
        running = spec.get("running")
        if type(enabled) is not bool or running is not None and type(running) is not bool:
            raise CompileError("invalid service state")
        if not enabled and running is True:
            raise CompileError("a disabled service cannot be required to run")
        if ((service in ownership.get("requiredServices", []) and (not enabled or running is False))
                or (service in ownership.get("forbiddenServices", []) and enabled)):
            raise CompileError("service state violates resource ownership policy")
        action = spec.get("action", "reload")
        if action not in ("reload", "restart"):
            raise CompileError("unsupported service activation action")
        triggers = spec.get("reloadOn", {"uci": [], "files": []})
        services[service] = {"enable": enabled, "running": running if enabled else False,
                             "oneshot": spec.get("oneshot", False) or running is None,
                             "reloadOn": triggers, "action": action}
        for package in triggers.get("uci", []):
            if not NAME.fullmatch(package) or "etc/config/" + package not in files:
                raise CompileError("service reload trigger references an unmanaged UCI package")
        for path in triggers.get("files", []):
            if not isinstance(path, str) or not path.startswith("/") or path[1:] not in files:
                raise CompileError("service reload trigger references an unmanaged file")
    return {"files": files, "modes": modes, "services": services}


def validate_checks(node, resources):
    result = []
    checks = node.get("checks", {})
    if not isinstance(checks, dict) or len(checks) > 64:
        raise CompileError("checks must be a named map of at most 64 checks")
    for name, spec in sorted(checks.items()):
        if not NAME.fullmatch(name) or len(name) > 64 or not isinstance(spec, dict):
            raise CompileError("invalid check name or specification")
        phase, executable = spec.get("phase"), spec.get("executable")
        timeout = spec.get("timeoutSeconds", 30)
        if phase not in ("compile", "preflight", "preconfirm", "postrollback"):
            raise CompileError("invalid check phase")
        if type(timeout) is not int or not 1 <= timeout <= 120:
            raise CompileError("check timeout must be between 1 and 120 seconds")
        if not isinstance(executable, str) or not executable.startswith("/") or any(c in executable for c in "\x00\n\r"):
            raise CompileError("check requires an absolute executable path")
        if phase == "compile":
            if not executable.startswith("/nix/store/"):
                raise CompileError("compile check executable must be an immutable Nix store artifact")
            sha256 = None
        else:
            rel = validate_path(executable)
            if not rel.startswith("usr/libexec/wrtix-managed/") or rel not in resources["files"]:
                raise CompileError("target check must be a supplied wrtix-managed executable")
            if resources["modes"].get(rel) not in ("700", "755"):
                raise CompileError("target check file must be executable")
            sha256 = digest(resources["files"][rel])
        result.append({"name": name, "phase": phase, "executable": executable, "sha256": sha256,
                       "timeoutSeconds": timeout, "input": spec.get("input", {})})
    return result


def resource_view(resources):
    return {
        "uci": {path[len("etc/config/"):]: parse_uci(data) for path, data in resources["files"].items()
                if path.startswith("etc/config/")},
        "files": {path: {"content_base64": base64.b64encode(data).decode("ascii"), "mode": resources["modes"][path]}
                  for path, data in resources["files"].items() if not path.startswith("etc/config/")},
        "services": resources["services"],
    }


def run_compile_checks(checks, device_id, resources, fleet):
    for check in checks:
        if check["phase"] != "compile":
            continue
        payload = json.dumps({"input": check["input"], "device_id": device_id,
                              "resources": resource_view(resources), "fleet": fleet}, ensure_ascii=False).encode()
        try:
            with subprocess.Popen([check["executable"]], stdin=subprocess.PIPE,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  start_new_session=True) as process:
                try:
                    process.communicate(payload, timeout=check["timeoutSeconds"])
                except BaseException:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate()
                    raise
            if process.returncode:
                raise CompileError("configuration check failed: " + check["name"])
        except (OSError, subprocess.SubprocessError):
            raise CompileError("configuration check unavailable or timed out: " + check["name"]) from None


def merge_resource_sets(node):
    """Merge named producers symmetrically, retaining ownership until validation."""
    sets = node.get("resourceSets", {})
    if not isinstance(sets, dict) or any(not NAME.fullmatch(name) or not isinstance(value, dict)
                                        for name, value in sets.items()):
        raise CompileError("resourceSets must be a named map of resource producers")
    sources = [("set:" + name, value) for name, value in sorted(sets.items())] + [("caller", node)]
    result = {key: copy.deepcopy(value) for key, value in node.items()
              if key not in ("resourceSets", "uci", "files", "services", "packages", "checks", "ownership", "overrides", "users", "resolver", "identity")}
    for key in ("uci", "files", "services", "checks", "users", "identity"):
        result[key] = {}
    result.update(packages=[], resolver=None, ownership={}, overrides={"uci": {}})
    exclusive, protected_packages, protected_options = {}, {}, {}
    for owner, value in sources:
        policy = value.get("ownership", {})
        for field, target in (("exclusivePackages", exclusive), ("protectedPackages", protected_packages)):
            for package in policy.get(field, []):
                if package in target and target[package] != owner:
                    raise CompileError("resource ownership is claimed by multiple producers")
                target[package] = owner
        for package, options in policy.get("protectedOptions", {}).items():
            for option in options:
                key = (package, option)
                if key in protected_options and protected_options[key] != owner:
                    raise CompileError("resource option ownership is claimed by multiple producers")
                protected_options[key] = owner
    for owner, value in sources:
        for package, sections in value.get("uci", {}).items():
            if not NAME.fullmatch(package):
                raise CompileError("invalid UCI package name")
            if package in exclusive and exclusive[package] != owner:
                raise CompileError("UCI package conflicts with its exclusive owner")
            target = result["uci"].setdefault(package, {})
            for section, spec in sections.items():
                if not SECTION.fullmatch(section) or not isinstance(spec, dict):
                    raise CompileError("invalid UCI section")
                if section not in target:
                    target[section] = {"type": spec["type"], "anonymous": spec.get("anonymous", False),
                                       "order": spec.get("order", 0), "options": {}}
                existing = target[section]
                if any(existing[key] != spec.get(key, default) for key, default in (("type", None), ("anonymous", False), ("order", 0))):
                    raise CompileError("UCI section declarations conflict")
                for option, content in spec.get("options", {}).items():
                    if ((package in protected_packages and protected_packages[package] != owner)
                            or ((package, option) in protected_options and protected_options[package, option] != owner)):
                        raise CompileError("UCI option conflicts with its protected owner")
                    if option in existing["options"] and existing["options"][option] != content:
                        raise CompileError("UCI field has conflicting producer values; use an explicit override")
                    existing["options"][option] = copy.deepcopy(content)
        for key in ("files", "services", "checks"):
            for name, spec in value.get(key, {}).items():
                if name in result[key]:
                    if key != "services" or result[key][name] != spec:
                        raise CompileError(key + " resource has multiple owners or conflicting definitions")
                else:
                    result[key][name] = copy.deepcopy(spec)
        for package in value.get("packages", []):
            if package not in result["packages"]:
                result["packages"].append(package)
        for user, fields in value.get("users", {}).items():
            target = result["users"].setdefault(user, {})
            for field, content in fields.items():
                if content is None:
                    continue
                if field in target and target[field] != content:
                    raise CompileError("user credential has conflicting producer definitions")
                target[field] = copy.deepcopy(content)
        resolver = value.get("resolver")
        if resolver is not None:
            if result["resolver"] is not None and result["resolver"] != resolver:
                raise CompileError("resolver has conflicting producer definitions")
            result["resolver"] = copy.deepcopy(resolver)
        for key, content in value.get("identity", {}).items():
            if key in result["identity"] and result["identity"][key] != content:
                raise CompileError("identity has conflicting producer expectations")
            result["identity"][key] = copy.deepcopy(content)
        policy = value.get("ownership", {})
        for field in ("exclusivePackages", "protectedPackages", "requiredServices", "forbiddenServices"):
            result["ownership"].setdefault(field, [])
            result["ownership"][field] = sorted(set(result["ownership"][field]) | set(policy.get(field, [])))
        for package, options in policy.get("protectedOptions", {}).items():
            target = result["ownership"].setdefault("protectedOptions", {}).setdefault(package, [])
            result["ownership"]["protectedOptions"][package] = sorted(set(target) | set(options))
        for package, sections in value.get("overrides", {}).get("uci", {}).items():
            target = result["overrides"]["uci"].setdefault(package, {})
            for name, spec in sections.items():
                if name in target and target[name] != spec:
                    raise CompileError("explicit UCI overrides conflict")
                target[name] = copy.deepcopy(spec)
    for service in result["ownership"].get("requiredServices", []):
        desired = result["services"].get(service, {})
        if not desired.get("enable", False) or desired.get("running") is False:
            raise CompileError("required producer service is missing or disabled")
    return result


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def compile_fleet(manifest, resolve_secrets=False, resolver=None):
    if manifest.get("schema_version") != 1 or manifest.get("kind") != "openwrt-fleet":
        raise CompileError("unsupported fleet manifest")
    raw_devices = manifest.get("devices")
    if not isinstance(raw_devices, dict) or not raw_devices or any(not isinstance(n, dict) for n in raw_devices.values()):
        raise CompileError("fleet must contain devices")
    effective = {name: {key: value for key, value in node.items() if key != "profiles"}
                 for name, node in raw_devices.items()}
    refs = {name: SecretResolver.references(node) for name, node in effective.items()}
    if resolve_secrets:
        if resolver is None:
            resolver = SecretResolver.from_config(manifest.get("decryption", {}))
            try:
                nodes = resolver.resolve(effective)
            finally:
                resolver.close()
        else:
            nodes = resolver.resolve(effective)
    else:
        nodes = placeholder(effective)
    contract, contract_hash = load_contract()
    reserved = set(contract.get("reserved_services", ["wrtix-agent"]))
    nodes = {name: merge_resource_sets(node) for name, node in nodes.items()}
    prepared = {name: prepare_resources(node, reserved) for name, node in nodes.items()}
    fleet_view = {name: {"identity": node.get("identity", {}), **resource_view(prepared[name])}
                  for name, node in nodes.items()}
    result = {}
    for name, node in nodes.items():
        if not SECTION.fullmatch(name) or len(name) > 64 or node.get("id", name) != name:
            raise CompileError("invalid stable device identifier")
        resources = prepared[name]
        files, modes = resources["files"], resources["modes"]
        checks = validate_checks(node, resources)
        if resolve_secrets:
            run_compile_checks(checks, name, resources, fleet_view)
        user = node.get("users", {}).get("root", {})
        hash_ = user.get("passwordHash")
        if hash_ is not None:
            if not resolve_secrets:
                hash_ = SENTINEL_HASH
            if not isinstance(hash_, str) or not re.fullmatch(r"\$(?:5|6|y)\$[A-Za-z0-9./$=,-]+", hash_) or len(hash_) > 1024:
                raise CompileError("invalid root password hash")
            files["etc/wrtix/root-password-hash"] = (hash_ + "\n").encode()
            modes["etc/wrtix/root-password-hash"] = "600"
        keys = user.get("authorizedKeys")
        if keys is not None:
            if not isinstance(keys, list) or any(not isinstance(k, str) or not k.strip() or any(c in k for c in "\n\r\x00") for k in keys):
                raise CompileError("authorized keys must be a list of single-line public keys")
            if "etc/dropbear/authorized_keys" in files:
                raise CompileError("authorized keys have multiple owners")
            files["etc/dropbear/authorized_keys"] = ("\n".join(keys) + ("\n" if keys else "")).encode()
            modes["etc/dropbear/authorized_keys"] = "600"
        files["etc/wrtix/identity.json"] = canonical({"schema_version": 1, "device_id": name})
        modes["etc/wrtix/identity.json"] = "600"
        if len(files) > 128 or any(len(data) > 131072 for data in files.values()):
            raise CompileError("configuration exceeds transaction resource limits")
        resolver_spec = node.get("resolver")
        target = resolver_spec.get("target") if isinstance(resolver_spec, dict) else None
        if resolver_spec is not None and (target is None or not re.fullmatch(r"/[A-Za-z0-9_./-]+", target)
                or any(part in ("", ".", "..") for part in target[1:].split("/"))):
            raise CompileError("resolver requires an explicit absolute symlink target")
        packages = node.get("packages", [])
        if not isinstance(packages, list) or any(not isinstance(p, str) or not NAME.fullmatch(p) for p in packages):
            raise CompileError("invalid required package name")
        expected = node.get("identity", {})
        if not isinstance(expected, dict) or set(expected) - {"board_names"}:
            raise CompileError("unsupported expected device identity fields")
        boards = expected.get("board_names", [])
        if not isinstance(boards, list) or any(not isinstance(b, str) or not re.fullmatch(r"[A-Za-z0-9_,.+-]+", b) for b in boards):
            raise CompileError("invalid expected board names")
        result[name] = {**resources, "packages": packages,
                       "root_password_managed": hash_ is not None,
                       "identity": {"device_id": name, "board_names": boards},
                       "contract_sha256": contract_hash, "resolver_target": target,
                       "target_host": node.get("target_host") or name, "ssh_port": node.get("ssh_port", 22),
                       "checks": [check for check in checks if check["phase"] != "compile"],
                       "secret_references": refs[name], "resolved_secrets": resolve_secrets,
                       "preserve_paths": [] if keys is not None else ["etc/dropbear/authorized_keys"]}
    return result
