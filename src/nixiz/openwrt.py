#!/usr/bin/env python3
# This file was generated entirely by AI.
# It has not been reviewed line by line by a human.
"""Deploy declarative OpenWrt configuration; decrypt secrets only at execution."""
from __future__ import annotations

import argparse
import contextlib
import copy
import getopt
import hashlib
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import shlex
import signal
import subprocess
import sys
import tarfile
import tempfile
import time

from compiler import CompileError, compile_fleet, digest
from secret_refs import SecretError, SecretResolver, duplicate_free
from progress import Progress
from receipts import ReceiptError, allocate_receipt, clean_receipts, default_directory, list_receipts, receipt_lock

HERE = Path(__file__).resolve().parent
REBOOT_INPUT_SECONDS = 150
REMOTE = "/usr/libexec/nixiz/agent"
GUARD = "/etc/init.d/nixiz-agent"
LEDGER = "etc/nixiz/managed.json"


class DeployError(RuntimeError):
    pass


class ActivationError(DeployError):
    """A verified terminal transaction failure, never a transient SSH failure."""


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def read_json(path):
    try:
        data = Path(path).read_bytes()
        if len(data) > 2 * 1024 * 1024:
            raise DeployError("input is too large")
        return json.loads(data, object_pairs_hook=duplicate_free)
    except (OSError, ValueError):
        raise DeployError("configuration or receipt is unavailable or invalid") from None


def private_write(path, data, replace=True):
    path = Path(path)
    with receipt_lock(path):
        if path.is_symlink() or (not replace and path.exists()):
            raise DeployError("refusing existing or symbolic-link receipt")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                os.replace(temporary, path)
            else:
                # Publish a newly allocated receipt without overwriting a file
                # concurrently created by another deployment at an explicit path.
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    raise DeployError("refusing existing or symbolic-link receipt") from None
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def load_manifest(args):
    if not args.manifest:
        raise DeployError("select --manifest or use openwrt-rebuild to build a configuration")
    return read_json(args.manifest)


# Only fixed protocol codes from the bundled runtime may appear in diagnostics.
# Unknown stderr, including plugin output, is never copied to the terminal.
REMOTE_ERROR_CODES = frozenset("""
already_applied already_confirmed check_failed archive_exists backup_paths_failed
baseline_changed boot_identity_unavailable bundle_too_large confirmation_readback_mismatch
corrupt_backup deletion_not_owned duplicate_archive_member engine_version_changed
expired_transaction file_snapshot_failed firmware_operation_active guard_not_ready
guard_start_failed guard_version_changed incoming_exists init_service_links_present
init_service_must_be_disabled init_service_must_be_stopped installer_archive_too_large
installer_guard_copy_failed installer_guard_workspace_failed installer_hash_failed
installer_invalid_archive installer_identity_status_failed installer_lock_failed
installer_nonregular_archive_member installer_runtime_hash_mismatch
installer_runtime_status_failed installer_transaction_active installer_transfer_failed
installer_unexpected_archive_member installer_unsafe_directory installer_unsafe_lock
installer_unsafe_runtime installer_workspace_failed invalid_archive invalid_backup_metadata
invalid_backup_paths invalid_check_request invalid_command invalid_lease invalid_manifest
invalid_owned_ledger invalid_payload_name invalid_service_backup invalid_token
identity_check_failed identity_status_failed transaction_active missing_contract missing_agent_runtime
missing_backup_metadata missing_guard missing_manifest missing_payload missing_required_service
no_transaction nonregular_archive_member orphan_reload_child_would_not_stop payload_corrupt
payload_mismatch pending_config_check_failed pending_uci_changes reload_child_would_not_stop
reload_not_complete reload_worker_would_not_stop request_too_large resolver_backup_corrupt
resolver_snapshot_failed restored_network_not_ready root_password_sync_failed
service_disable_failed service_enable_failed service_link_conflict service_link_read_failed
service_readback_failed service_reload_failed service_restore_failed
service_restore_readback_failed service_snapshot_failed service_start_failed
service_state_unavailable service_stop_failed snapshot_workspace_failed token_mismatch reload_worker_failed
transaction_active unexpected_archive_member unsafe_archive_directory unsafe_backup_path
unsafe_existing_path unsafe_install_path unsafe_lock unsafe_path unsafe_resolver_type
unsafe_restore_path unsafe_runtime_directory unsafe_service_link unsafe_service_name
unsafe_state_directory unsupported_resolver_symlink wireless_reload_failed
unsafe_initialization_access
initialization_access_snapshot_failed initialization_key_retirement_failed confirmation_identity_mismatch
initialization_access_changed network_mac_check_failed network_mac_config_invalid
invalid_network_restart_state invalid_reboot_state reboot_not_required reboot_already_consumed
reboot_readback_mismatch invalid_resume_request resume_boot_mismatch invalid_resume_budget
resume_workspace_failed no_reboot_to_resume post_reboot_readback_failed reboot_required
wireless_mac_check_failed wireless_mac_config_invalid
agent_check_failed invalid_checks check_unavailable preflight_check_failed preconfirm_check_failed postrollback_check_failed
""".split())



def interactive_terminal():
    """SSH reads host confirmation from the foreground controlling terminal."""
    try:
        terminal = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        try:
            return os.tcgetpgrp(terminal) == os.getpgrp()
        finally:
            os.close(terminal)
    except OSError:
        return False


def remote_failure(stderr, returncode):
    """Classify failures using fixed messages; stderr may contain credentials."""
    stderr = stderr or b""
    if returncode == 255:
        lowered = stderr.lower()
        categories = (
            ((b"remote host identification has changed",),
             "SSH host key changed; verify the replacement key before updating known_hosts"),
            ((b"host key verification failed",),
             "SSH host key verification failed; confirm new keys from an interactive terminal or provide trusted known_hosts"),
            ((b"permission denied", b"too many authentication failures"),
             "SSH authentication failed; check SSH_AUTH_SOCK and the target authorized keys"),
            ((b"connection refused",), "SSH connection refused"),
            ((b"connection timed out", b"operation timed out"), "SSH connection timed out"),
            ((b"no route to host", b"network is unreachable"), "SSH target is unreachable"),
            ((b"could not resolve hostname", b"name or service not known", b"temporary failure in name resolution"),
             "SSH hostname lookup failed"),
            ((b"connection reset by peer", b"connection closed by", b"broken pipe"),
             "SSH connection was closed"),
        )
        for markers, message in categories:
            if any(marker in lowered for marker in markers):
                return message
    reasons = re.findall(rb"(?m)^openwrt-deploy: ([a-z_]{1,64})\r?$", stderr)
    for reason in reversed(reasons):
        code = reason.decode()
        if code in REMOTE_ERROR_CODES:
            return "remote operation failed: " + code
    return "remote operation failed"


def ssh_options(words):
    """Preserve OpenSSH options, normalizing -J/-S to first-value-wins forms."""
    try:
        # OpenSSH's documented short options; option arguments remain single argv entries.
        parsed, rest = getopt.getopt(words, "46AaCfGgKkMNnqsTtVvXxYyB:b:c:D:E:e:F:I:i:J:L:l:m:O:o:P:p:Q:R:S:W:w:")
        if rest:
            raise ValueError()
    except (getopt.GetoptError, ValueError, TypeError):
        raise DeployError("invalid SSH options; provide options without a destination or command") from None
    result = []
    for option, value in parsed:
        if option in ("-J", "-S"):
            result += ["-o", ("ProxyJump=" if option == "-J" else "ControlPath=") + value]
        else:
            result.append(option)
            if value or option[1:] in "BbcDEeFIiJLlmOoPpQRSWw":
                result.append(value)
    return result


def valid_unenrolled(identity, boot):
    return (isinstance(boot, str)
            and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot) is not None
            and identity.get("device_id") == "")


class Transport:
    def __init__(self, args, manifest, compiled, node, host=None):
        self.args, self.manifest, self.compiled, self.node = args, manifest, compiled, node
        target = host or getattr(args, "target_host", None) or getattr(args, "host", None) or compiled["target_host"]
        if not isinstance(target, str):
            raise DeployError("invalid target host")
        user, separator, address = target.rpartition("@")
        self.user, self.host = (user, address) if separator else ("root", target)
        self._explicit_user = bool(separator)
        if self.host.startswith("[") and self.host.endswith("]"):
            self.host = self.host[1:-1]
            if ":" not in self.host:
                raise DeployError("invalid target host")
        if ":" in self.host:
            self.host = self.host.replace("%25", "%")
            try:
                ipaddress.IPv6Address(self.host)
            except ValueError:
                raise DeployError("invalid target host") from None
        if (not re.fullmatch(r"[A-Za-z0-9_.:%-]+", self.host) or self.host.startswith("-")
                or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", self.user)):
            raise DeployError("invalid target host")
        self.port = next(value for value in (args.port, manifest.get("deployment", {}).get("port"),
                                             compiled["ssh_port"]) if value is not None)
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise DeployError("invalid target SSH port")
        try:
            self._environment_options = ssh_options(shlex.split(os.environ.get("NIX_SSHOPTS", "")))
        except ValueError:
            raise DeployError("invalid NIX_SSHOPTS quoting") from None
        self._options_checked = False
        self._control_directory = None
        self.host_key_alias = self.host if self.port == 22 else "[" + self.host + "]:" + str(self.port)
        self._pinned_host_key_alias = None
        self._private_connection = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        """Close only masters in our private directory, never a caller's socket."""
        directory, self._control_directory = self._control_directory, None
        if directory is None:
            return
        try:
            for path in Path(directory.name).glob("ssh-*"):
                if path.is_socket():
                    with contextlib.suppress(OSError, subprocess.SubprocessError):
                        subprocess.run(["ssh", "-S", str(path), "-O", "exit", "-o", "BatchMode=yes",
                                        "-l", self.user, self.host], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False,
                                       env=dict(os.environ, SSH_ASKPASS_REQUIRE="never"))
        finally:
            directory.cleanup()

    def destination(self):
        host = "[" + self.host + "]" if ":" in self.host else self.host
        return self.user + "@" + host

    def _endpoint(self):
        return self.destination() + ":" + str(self.port)

    def _run_stage(self, stage, command, data=None):
        try:
            return self.run(command, data=data)
        except DeployError as error:
            raise DeployError(stage + ": " + str(error)) from None

    def reconnect(self, host, host_key_alias=None, *, port=None):
        """Use a fresh connection and the original trusted host key at a new address."""
        args = copy.copy(self.args)
        args.control_socket, args.port = None, self.port if port is None else port
        target = host if "@" in host else self.user + "@" + host
        result = Transport(args, self.manifest, self.compiled, self.node, host=target)
        result._private_connection = True
        result._pinned_host_key_alias = host_key_alias or self.host_key_alias
        result.host_key_alias = result._pinned_host_key_alias
        return result

    def _private_control_options(self):
        if self._control_directory is None:
            self._control_directory = tempfile.TemporaryDirectory(prefix="owrt-ssh-", dir="/tmp")
        return ["-o", "ControlMaster=auto", "-o", "ControlPersist=60",
                "-o", "ControlPath=" + self._control_directory.name + "/ssh-%C"]

    def _ssh_options(self):
        interactive = interactive_terminal()
        options = ["ssh", "-T"]
        # OpenSSH handles existing keys, agents and interactive password prompts.
        if not interactive:
            options += ["-o", "BatchMode=yes"]
        if self._private_connection:
            # A supplied initial socket must never impersonate the final address.
            options += self._private_control_options()
            try:
                ipaddress.ip_address(self.host)
            except ValueError:
                pass
            else:
                options += ["-o", "HostName=" + self.host]
        if self._pinned_host_key_alias:
            options += ["-o", "HostKeyAlias=" + self._pinned_host_key_alias,
                        "-o", "StrictHostKeyChecking=yes"]
        if self._explicit_user:
            options += ["-l", self.user]
        if self.args.port is not None:
            options += ["-p", str(self.args.port)]
        if self.args.control_socket:
            options += ["-o", "ControlPath=" + self.args.control_socket]
        if self.args.jump_host:
            options += ["-o", "ProxyJump=" + self.args.jump_host]
        if self.args.known_hosts:
            options += ["-o", "UserKnownHostsFile=" + self.args.known_hosts, "-o", "StrictHostKeyChecking=yes"]
        options += self._environment_options
        options += ssh_options(self.manifest.get("deployment", {}).get("sshOptions", []))
        options += ["-l", self.user, "-p", str(self.port), "-o", "ServerAliveInterval=20",
                    "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=8"]
        if not self.args.control_socket:
            # A short, private path also avoids the Unix socket path limit.
            options += self._private_control_options()
        if interactive:
            # Explicit NIX_SSHOPTS/deployment.sshOptions may still request BatchMode=yes.
            options += ["-o", "BatchMode=no"]
        return options

    def _check_options(self, options):
        if self._options_checked:
            return
        if self._environment_options or self.manifest.get("deployment", {}).get("sshOptions"):
            # Let OpenSSH interpret -p/-oPort, -l/-oUser and ssh_config itself.
            # -G evaluates configuration without establishing an authenticated connection.
            try:
                result = subprocess.run([*options, "-G", self.host], input=b"", stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, timeout=10, check=False,
                                        env=dict(os.environ, SSH_ASKPASS_REQUIRE="never"))
                values = dict(line.split(" ", 1) for line in result.stdout.decode().splitlines() if " " in line)
                port, user = int(values["port"]), values["user"]
                if result.returncode or not 1 <= port <= 65535 or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", user):
                    raise ValueError()
            except (OSError, subprocess.SubprocessError, ValueError, KeyError, UnicodeError):
                raise DeployError("SSH options could not be evaluated for " + self._endpoint()) from None
            self.port, self.user = port, user
            name = values.get("hostname", self.host)
            self.host_key_alias = values.get("hostkeyalias") or (name if port == 22 else "[" + name + "]:" + str(port))
        self._options_checked = True

    def run(self, command, data=None, timeout=60):
        options = self._ssh_options()
        self._check_options(options)
        try:
            # Native SSH host-trust/passphrase questions use /dev/tty. Suspend
            # animation while opening a connection so it cannot erase a prompt.
            terminal = Progress.external_output() if interactive_terminal() else contextlib.nullcontext()
            with terminal:
                proc = subprocess.run(options + [self.host, command], input=data if data is not None else b"",
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False,
                                      env=dict(os.environ, SSH_ASKPASS_REQUIRE="never"))
        except subprocess.TimeoutExpired:
            raise DeployError("SSH operation timed out for " + self._endpoint()
                              + " (timeout " + str(timeout) + "s)") from None
        except OSError:
            raise DeployError("SSH client could not be started for " + self._endpoint()) from None
        if proc.returncode:
            message = remote_failure(proc.stderr, proc.returncode)
            raise DeployError(message + " [target " + self._endpoint()
                              + ", exit " + str(proc.returncode) + "]")
        return proc.stdout

    def call(self, action, token=None, data=None):
        if action not in {"check", "verify", "stage", "apply", "status", "confirm", "rollback", "reboot", "resume"}:
            raise DeployError("invalid transaction command")
        if token is not None and not re.fullmatch(r"[0-9a-f]{32}", token):
            raise DeployError("invalid receipt token")
        result = self._run_stage(action, shlex.join(["sh", REMOTE, action] + ([] if token is None else [token])), data=data)
        try:
            return json.loads(result)
        except ValueError:
            raise DeployError("invalid transaction response") from None

    def agent_contract(self):
        output = self._run_stage("agent identity preflight", shlex.join(["sh", REMOTE, "identity"]))
        try:
            result = json.loads(output)
            if (not isinstance(result, dict) or result.get("profile") != "nixiz-v2"
                    or not isinstance(result.get("identity"), dict)
                    or any(not isinstance(result.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", result[key])
                           for key in ("contract_sha256", "engine_sha256", "guard_sha256"))):
                raise ValueError()
            return result
        except (ValueError, UnicodeError):
            raise DeployError("target nixiz contract is unavailable or incompatible") from None

    def agent_preflight(self, allow_initialize=False):
        result = self.agent_contract()
        identity, desired = result["identity"], self.compiled["identity"]
        if allow_initialize and not identity.get("device_id"):
            if not valid_unenrolled(identity, result.get("boot_id")):
                raise DeployError("target does not prove an unenrolled state")
        elif identity.get("device_id") != self.node:
            raise DeployError("target registered identity differs")
        if desired.get("board_names") and identity.get("board_name") not in desired["board_names"]:
            raise DeployError("target board differs from the declared expectation")
        if result.get("contract_sha256") != self.compiled["contract_sha256"]:
            raise DeployError("target agent protocol differs from the controller")
        self.compiled["engine_sha256"] = result["engine_sha256"]
        self.compiled["guard_sha256"] = result["guard_sha256"]
        return result

    def installed(self):
        self.agent_contract()
        return True

    def ensure_engine(self):
        # The package owns its runtime. Deployment must never overwrite it.
        result = self.agent_contract()
        if result["contract_sha256"] != self.compiled["contract_sha256"]:
            raise DeployError("target agent protocol differs from the controller")
        self.compiled.update({key: result[key] for key in ("engine_sha256", "guard_sha256")})

    def ledger(self):
        data = self._run_stage("managed resource ledger read", "if test -f /" + LEDGER + " && test ! -L /" + LEDGER + "; then cat /" + LEDGER + "; else printf '{}'; fi")
        try:
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except ValueError:
            raise DeployError("managed-resource ledger is invalid") from None


def service_differences(compiled, baseline, old):
    different = []
    for name, desired in compiled["services"].items():
        actual = baseline.get("services", {}).get(name, {})
        if not actual.get("present") and not desired["enable"]:
            continue
        if (actual.get("enabled") != desired["enable"]
                or desired.get("running") is not None and actual.get("running") != desired["running"]):
            different.append(name)
    different.extend(sorted(set(old.get("services", [])) - set(compiled["services"])))
    return sorted(set(different))


def pending_bundle(compiled, baseline, node, ttl, old, initialize=False):
    token = secrets.token_hex(16)
    files = []
    payload = {}
    for index, (path, data) in enumerate(sorted(compiled["files"].items())):
        item = "payload/" + str(index)
        files.append({"path": path, "mode": compiled["modes"][path], "sha256": digest(data),
                      "size": len(data), "payload": item, "delete": False})
        payload[item] = data
    for path in sorted(set(old.get("files", [])) - set(compiled["files"]) - set(compiled.get("preserve_paths", []))):
        files.append({"path": path, "mode": None, "sha256": None, "size": 0, "payload": None, "delete": True})
    services = {name: dict(spec) for name, spec in compiled["services"].items()}
    for name in set(old.get("services", [])) - set(services):
        services[name] = {"enable": False, "running": False, "action": "reload"}
    changed = changed_files(compiled, baseline, old)
    service_changes = service_differences(compiled, baseline, old)
    for name, desired in services.items():
        triggers = desired.pop("reloadOn", {})
        desired["reload"] = name in service_changes or any(
            path in changed for path in ["etc/config/" + key for key in triggers.get("uci", [])]
            + [path.lstrip("/") for path in triggers.get("files", [])])
    manifest = {"schema_version": 1, "kind": "nixiz-transaction", "token": token,
                "boot_id": baseline["boot_id"], "device_id": node,
                "board_names": compiled["identity"].get("board_names", []),
                "contract_sha256": compiled["contract_sha256"], "resolver_target": compiled.get("resolver_target"), "ttl_seconds": ttl,
                "initialize": initialize, "baseline": baseline, "files": files, "services": services, "managed_services": sorted(compiled["services"]), "changed_paths": changed,
                "engine_sha256": compiled["engine_sha256"],
                "guard_sha256": compiled["guard_sha256"],
                "root_password_managed": compiled.get("root_password_managed", False),
                "checks": compiled.get("checks", [])}
    payload = {"manifest.json": canonical(manifest), **payload}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for path, data in payload.items():
            info = tarfile.TarInfo(path)
            info.mode = 0o600
            info.uid = info.gid = 0
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return manifest, buffer.getvalue()


def managed_paths(compiled, old):
    return sorted(set(compiled["files"]) | (set(old.get("files", [])) - set(compiled.get("preserve_paths", []))))


def changed_files(compiled, baseline, old):
    changed = [path for path, data in compiled["files"].items()
               if baseline.get("files", {}).get(path) != digest(data)
               or baseline.get("modes", {}).get(path) != compiled["modes"][path]]
    changed += sorted(set(old.get("files", [])) - set(compiled["files"]) - set(compiled.get("preserve_paths", [])))
    return sorted(set(changed))


def safe_reload_error(value, phase):
    """Only protocol metadata may enter receipts or user-facing diagnostics."""
    if (not isinstance(value, dict) or value.get("phase") != phase
            or not isinstance(value.get("code"), str) or value["code"] not in REMOTE_ERROR_CODES):
        return None
    if value.get("kind") not in (None, "service", "wireless", "worker"):
        return None
    service = value.get("service")
    if service is not None and (not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,63}", service)):
        return None
    if value.get("action") not in (None, "start", "stop", "enable", "disable", "reload", "restart"):
        return None
    code = value.get("exit_code")
    if code is not None and (type(code) is not int or not 0 <= code <= 255):
        return None
    if value.get("exit_kind") not in (None, "timeout", "timeout_error", "not_executable", "not_found", "killed", "terminated", "exit"):
        return None
    return {key: value.get(key) for key in ("phase", "code", "kind", "service", "action", "exit_code", "exit_kind")}


def activation_failure(status, *, initialize=False):
    details = []
    for field, phase in (("activation_error", "activate"), ("restore_error", "restore")):
        error = safe_reload_error(status.get(field), phase)
        if error is None:
            continue
        text = phase + ": " + error["code"]
        if error["kind"]:
            text += ", " + error["kind"]
        for key in ("service", "action"):
            if error[key]:
                text += " " + error[key]
        if error["exit_code"] is not None:
            text += ", exit " + str(error["exit_code"])
            if error["exit_kind"]:
                text += " (" + error["exit_kind"] + ")"
        details.append(text)
    message = ("initialization" if initialize else "transaction") + " rolled back or service activation failed"
    return ActivationError(message + (" [" + "; ".join(details) + "]" if details else ""))


def wait_ready(transport, token, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            status = transport.call("status")
            if status.get("token") != token:
                raise DeployError("transaction identity changed")
            if status.get("state") in ("rolled-back", "rollback_pending", "rollback_incomplete", "reload_failed"):
                raise activation_failure(status)
            if status.get("reload_complete"):
                return status
        except DeployError as error:
            if isinstance(error, ActivationError) or str(error) == "transaction identity changed":
                raise
        time.sleep(2)
    raise DeployError("activation not verified; automatic rollback remains armed")


def transaction_status(status, receipt):
    if (status.get("token") != receipt.get("token")
            or status.get("device_id") != receipt.get("device_id")):
        raise DeployError("receipt transaction identity differs")
    origin = receipt.get("origin_boot_id", receipt.get("boot_id"))
    if "origin_boot_id" in status and status["origin_boot_id"] != origin:
        raise DeployError("transaction origin boot changed")
    if receipt.get("reboot_required") and not status.get("reboot_required"):
        raise DeployError("transaction reboot requirement changed")
    if not status.get("reboot_required"):
        return
    if status.get("origin_boot_id") != origin:
        raise DeployError("transaction origin boot changed")
    if status.get("state") in ("confirmed", "rolled-back", "rollback_pending", "rollback_incomplete", "reload_failed"):
        return
    phase, accepted, current = (status.get(key) for key in ("reboot_state", "boot_id", "current_boot_id"))
    if not all(isinstance(value, str) and re.fullmatch(
            r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value) for value in (accepted, current)):
        raise DeployError("invalid transaction boot identity")
    if phase in ("required", "armed"):
        valid = accepted == current == origin
    elif phase == "awaiting-resume":
        valid = accepted == origin and current != origin
    elif phase == "resumed":
        valid = accepted == current and accepted != origin
        if receipt.get("boot_id") != origin:
            valid = valid and accepted == receipt.get("boot_id")
    else:
        valid = False
    if not valid:
        raise DeployError("unexpected transaction boot")


def initialization_status(status, receipt):
    try:
        transaction_status(status, receipt)
        if not status.get("reboot_required") and status.get("boot_id") != receipt.get("boot_id"):
            raise DeployError("initialization boot changed")
    except DeployError:
        raise DeployError("initialization transaction identity changed") from None


def save_transaction_status(path, receipt, status, transport):
    transaction_status(status, receipt)
    receipt.update({"state": status.get("state"), "host": transport.host,
                    "port": transport.port, "user": transport.user})
    for key in ("reboot_required", "reboot_state", "current_boot_id"):
        if key in status:
            receipt[key] = status[key]
    for field, phase in (("activation_error", "activate"), ("restore_error", "restore")):
        error = safe_reload_error(status.get(field), phase)
        if error is not None:
            receipt[field] = error
    if status.get("reboot_state") == "resumed":
        receipt["boot_id"] = status["boot_id"]
    private_write(path, canonical(receipt))


def remaining_rollback_seconds(receipt):
    deadline = receipt.get("rollback_deadline")
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise DeployError("planned reboot requires a receipt with its original rollback deadline")
    remaining = int(deadline - time.time())
    if remaining <= 0:
        raise DeployError("transaction deadline passed; automatic rollback remains armed")
    return remaining


def receipt_port(receipt, key, fallback):
    value = receipt.get(key, fallback)
    if type(value) is not int or not 1 <= value <= 65535:
        raise DeployError("invalid receipt SSH port")
    return value


def receipt_endpoints(args, receipt):
    """Keep original, current and candidate ports distinct during recovery."""
    current = receipt_port(receipt, "port", 22)
    initial = receipt_port(receipt, "initial_port", current)
    post = receipt_port(receipt, "post_switch_port", current)
    override = getattr(args, "_receipt_port_override", None)
    post_host = receipt.get("post_switch_host") or receipt.get("management_host") or receipt["host"]
    explicit_post = getattr(args, "post_switch_port", None) is not None
    if override is not None:
        current = initial = override
        if args.command != "confirm" or not explicit_post:
            post = override
    if args.target_host:
        return [(args.target_host, post if args.command == "confirm" else current)]
    if args.command == "confirm":
        rows = [(post_host, post)]
        # A controller lost before requesting a planned reboot may still need
        # the old endpoint to issue it. It cannot confirm there instead.
        if receipt.get("reboot_required") and receipt.get("reboot_state") in ("required", "armed"):
            rows.append((receipt.get("initial_host", receipt["host"]), initial))
    else:
        rows = [(receipt["host"], current), (post_host, post),
                (receipt.get("initial_host", receipt["host"]), initial)]
    return list(dict.fromkeys(rows))


def verify_reboot_fallback(args, endpoint_index, status):
    # A stale receipt may say reboot is pending even when it already happened.
    # Only a live, applied pre-reboot transaction can use the old endpoint to
    # request that reboot; terminal or resumed state there is not confirmation.
    if args.command == "confirm" and endpoint_index > 0:
        if not (status.get("reboot_required") is True and status.get("state") == "applied"
                and status.get("reboot_state") in ("required", "armed")):
            raise DeployError("candidate SSH endpoint not verified; old endpoint cannot confirm")


def complete_planned_reboot(transport, receipt, receipt_path, status, transports, seconds, progress, *, ask_post_switch_host=False):
    """Spend the original lease across one boot, authenticating before resuming it."""
    transaction_status(status, receipt)
    if not status.get("reboot_required"):
        return transport, status
    deadline = time.monotonic() + min(seconds, remaining_rollback_seconds(receipt))
    terminal = {"rolled-back", "rollback_pending", "rollback_incomplete", "reload_failed", "confirmed"}
    fatal = {"receipt transaction identity differs", "transaction origin boot changed",
             "transaction reboot requirement changed", "invalid transaction boot identity",
             "unexpected transaction boot", "post-reboot agent boot identity differs",
             "target registered identity differs", "target board differs from the declared expectation",
             "target agent protocol differs from the controller"}
    def fatal_error(error):
        message = str(error)
        return isinstance(error, ActivationError) or message in fatal or any(reason in message for reason in (
            "remote operation failed: ", "SSH host key changed", "SSH host key verification failed"))
    if status.get("reboot_state") == "required" and status.get("state") not in terminal:
        receipt["reboot_requested_at"] = time.time()
    save_transaction_status(receipt_path, receipt, status, transport)
    if status.get("reboot_state") == "required" and status.get("state") not in terminal:
        progress.message("Rebooting target before configuration verification; automatic rollback remains armed")
        try:
            status = transport.call("reboot", receipt["token"])
            transaction_status(status, receipt)
        except DeployError as error:
            if fatal_error(error):
                raise
            progress.message("Reboot response unavailable; reconnecting to the transaction")
    if ask_post_switch_host and status.get("reboot_state") in ("required", "armed"):
        receipt["post_switch_host"] = prompt_post_switch_host(transport, receipt, deadline, after_reboot=True)
        private_write(receipt_path, canonical(receipt))
    # A new private master cannot reuse a caller's socket or a connection from the old boot.
    transport.close()
    destination = transport.host
    if status.get("reboot_state") in ("required", "armed"):
        destination = receipt.get("post_switch_host") or (
            receipt["management_host"] if receipt.get("initialize") else transport.host)
    transport = transports.enter_context(transport.reconnect(
        destination, host_key_alias=receipt.get("host_key_alias", transport.host_key_alias),
        port=receipt_port(receipt, "post_switch_port", transport.port)))
    previous = transports.enter_context(transport.reconnect(
        receipt.get("initial_host", transport.host), host_key_alias=receipt.get("host_key_alias", transport.host_key_alias),
        port=receipt_port(receipt, "initial_port", transport.port)))
    while time.monotonic() < deadline:
        try:
            status = transport.call("status")
            transaction_status(status, receipt)
            if status.get("state") in terminal:
                raise activation_failure(status)
            phase = status.get("reboot_state")
            if phase in ("awaiting-resume", "resumed"):
                contract = transport.agent_preflight()
                if contract.get("boot_id") != status.get("current_boot_id"):
                    raise DeployError("post-reboot agent boot identity differs")
                if phase == "awaiting-resume":
                    progress.message("Target reboot verified; resuming the original rollback deadline")
                    status = transport.call("resume", receipt["token"], data=canonical({
                        "boot_id": status["current_boot_id"],
                        "remaining_seconds": remaining_rollback_seconds(receipt)}))
                    transaction_status(status, receipt)
                save_transaction_status(receipt_path, receipt, status, transport)
                if status.get("reboot_state") == "resumed" and status.get("reload_complete"):
                    return transport, status
        except DeployError as error:
            if fatal_error(error) or str(error) in (
                    "transaction rolled back or service activation failed",
                    "transaction deadline passed; automatic rollback remains armed"):
                raise
        if (previous.host, previous.port) != (transport.host, transport.port):
            try:
                restored = previous.call("status")
                transaction_status(restored, receipt)
            except DeployError as error:
                if fatal_error(error):
                    raise
            else:
                if restored.get("state") in terminal:
                    save_transaction_status(receipt_path, receipt, restored, previous)
                    raise activation_failure(restored)
        time.sleep(2)
    raise DeployError("target reboot not verified; automatic rollback remains armed")




def prompt_post_switch_host(transport, receipt, deadline, *, after_reboot=False):
    """Read an address from the foreground TTY without consuming command stdin."""
    if not interactive_terminal():
        raise DeployError("--ask-post-switch-host requires a foreground controlling terminal; use --post-switch-host")
    requested = receipt.get("reboot_requested_at")
    if requested is not None:
        if type(requested) not in (int, float) or not math.isfinite(requested):
            raise DeployError("invalid reboot request time in receipt")
        # The target permits resume only within 180 seconds of boot. Count
        # conservatively from the request, leaving 30 seconds to authenticate.
        budget = max(0, REBOOT_INPUT_SECONDS - max(0, time.time() - requested))
        deadline = min(deadline, time.monotonic() + budget)
    elif after_reboot:
        deadline = min(deadline, time.monotonic() + REBOOT_INPUT_SECONDS)
    hint = ""
    with Progress.external_output():
        terminal = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        try:
            phase = "Reboot requested" if after_reboot else "Configuration activation requested"
            os.write(terminal, ("\n" + phase + ". " + hint + "Check DHCP leases for the current address.\n"
                "Waiting does not extend rollback or boot-recovery deadlines.\n").encode())
            while time.monotonic() < deadline:
                remaining = min(deadline - time.monotonic(), remaining_rollback_seconds(receipt))
                if remaining <= 0:
                    break
                os.write(terminal, ("Post-switch SSH address ([USER@]HOST; empty input cancels; "
                    + str(int(remaining)) + "s controller budget): ").encode())
                readable = []
                while time.monotonic() < deadline:
                    remaining = min(deadline - time.monotonic(), remaining_rollback_seconds(receipt))
                    if remaining <= 0:
                        break
                    readable, _, _ = select.select([terminal], [], [], min(1, remaining))
                    if readable:
                        break
                else:
                    readable = []
                if not readable:
                    break
                raw = os.read(terminal, 4096)
                if not raw or not raw.strip():
                    raise DeployError("post-switch address entry cancelled; automatic rollback remains armed")
                try:
                    if time.monotonic() >= deadline:
                        break
                    return transport.reconnect(raw.decode("ascii").strip()).destination()
                except (UnicodeError, DeployError):
                    os.write(terminal, b"Invalid SSH address; enter one host or IP address.\n")
        finally:
            os.close(terminal)
    raise DeployError("post-switch address input timed out; automatic rollback remains armed")


def wait_post_switch(initial, receipt, seconds, transports):
    """Preserve the old channel for a planned boot, otherwise verify the new one."""
    deadline = time.monotonic() + min(seconds, remaining_rollback_seconds(receipt))
    post = transports.enter_context(initial.reconnect(
        receipt.get("post_switch_host") or receipt.get("management_host") or initial.host, host_key_alias=receipt["host_key_alias"],
        port=receipt_port(receipt, "post_switch_port", initial.port)))
    terminal = {"rolled-back", "rollback_pending", "rollback_incomplete", "reload_failed"}
    while time.monotonic() < deadline:
        for transport in (initial, post):
            try:
                status = transport.call("status")
            except DeployError as error:
                if any(reason in str(error) for reason in (
                        "SSH host key changed", "SSH host key verification failed",
                        "remote operation failed: ", "invalid transaction response")):
                    raise
                continue
            transaction_status(status, receipt)
            if status.get("state") in terminal:
                raise activation_failure(status, initialize=receipt.get("initialize", False))
            if status.get("reboot_required"):
                if status.get("reboot_state") in ("required", "armed"):
                    if status.get("reload_complete"):
                        return transport, status
                    # MAC activation has not started; keep the original channel.
                    break
                if transport is post:
                    return transport, status
            elif transport is post and status.get("reload_complete"):
                current = status.get("current_boot_id", status.get("boot_id"))
                if current != receipt["boot_id"]:
                    raise DeployError("unexpected transaction boot")
                contract = transport.agent_preflight()
                if contract.get("boot_id") != current:
                    raise DeployError("post-switch agent boot identity differs")
                return transport, status
        time.sleep(2)
    raise DeployError("post-switch connection not verified; automatic rollback remains armed")


def verify_initialization_endpoint(status, receipt, contract):
    """Verify the original transaction at either endpoint, including rollback."""
    initialization_status(status, receipt)
    origin = receipt.get("origin_boot_id", receipt["boot_id"])
    if status.get("origin_boot_id") != origin:
        raise DeployError("initialization transaction identity changed")
    identity, desired = contract["identity"], receipt["expected_identity"]
    boards = desired.get("board_names", [])
    if ((boards and identity.get("board_name") not in boards)
            or contract.get("contract_sha256") != receipt["contract_sha256"]):
        raise DeployError("initialization recovery agent identity differs")
    current = status.get("current_boot_id")
    if not isinstance(current, str) or contract.get("boot_id") != current:
        raise DeployError("initialization boot changed")
    configured = identity.get("device_id") == receipt["device_id"]
    unenrolled = valid_unenrolled(identity, current) and identity == receipt.get("initial_identity")
    if not configured and not unenrolled:
        raise DeployError("initialization recovery device identity differs")


def wait_initialized(transport, receipt, seconds, *, initial_transport=None, receipt_path=None):
    deadline = time.monotonic() + seconds
    fatal = {"initialization transaction identity changed", "initialization boot changed",
             "initialization recovery agent identity differs", "initialization recovery device identity differs"}
    terminal = {"rolled-back", "rollback_pending", "rollback_incomplete", "reload_failed"}
    while time.monotonic() < deadline:
        try:
            status = transport.call("status")
            initialization_status(status, receipt)
            if status.get("state") in terminal:
                verify_initialization_endpoint(status, receipt, transport.agent_contract())
                if receipt_path is not None:
                    save_transaction_status(receipt_path, receipt, status, transport)
                raise activation_failure(status, initialize=True)
            contract = transport.agent_preflight()
            if contract.get("boot_id") != receipt["boot_id"]:
                raise DeployError("initialization boot changed")
            if status.get("reload_complete"):
                return status
        except DeployError as error:
            if isinstance(error, ActivationError) or str(error) in fatal:
                raise
            if initial_transport is not None and (initial_transport.host, initial_transport.port) != (transport.host, transport.port):
                try:
                    previous = initial_transport.call("status")
                    initialization_status(previous, receipt)
                    verify_initialization_endpoint(previous, receipt, initial_transport.agent_contract())
                except DeployError as recovery_error:
                    if str(recovery_error) in fatal:
                        raise
                else:
                    # The original endpoint can explain failure and remain usable
                    # for recovery. It can never satisfy enrollment's success test.
                    if previous.get("state") in terminal:
                        if receipt_path is not None:
                            save_transaction_status(receipt_path, receipt, previous, initial_transport)
                        raise activation_failure(previous, initialize=True)
        time.sleep(2)
    raise DeployError("initialization not verified at its declared management address; automatic rollback remains armed")


def initialization_recovery(args, receipt, transports):
    """Find the same pending target before or after its DHCP/MAC transition."""
    required = ("initial_host", "management_host", "host_key_alias", "contract_sha256", "configured_ssh_port")
    if any(key not in receipt for key in required) or not isinstance(receipt.get("expected_identity"), dict):
        raise DeployError("invalid initialization receipt")
    compiled = {"target_host": receipt["management_host"], "ssh_port": receipt["configured_ssh_port"],
                "identity": receipt["expected_identity"],
                "contract_sha256": receipt["contract_sha256"]}
    initial = Transport(args, {"deployment": {}}, compiled, receipt["device_id"],
                        host=receipt.get("user", "root") + "@" + receipt["initial_host"])
    last_error = None
    for endpoint_index, (host, port) in enumerate(receipt_endpoints(args, receipt)):
        transport = transports.enter_context(initial.reconnect(host, host_key_alias=receipt["host_key_alias"], port=port))
        try:
            current = transport.call("status")
            initialization_status(current, receipt)
            verify_reboot_fallback(args, endpoint_index, current)
            contract = transport.agent_contract()
            identity = contract["identity"]
            desired = receipt["expected_identity"]
            if ((desired.get("board_names") and identity.get("board_name") not in desired["board_names"])
                    or contract.get("contract_sha256") != receipt["contract_sha256"]):
                raise DeployError("initialization recovery agent identity differs")
            configured = identity.get("device_id") == receipt["device_id"]
            unenrolled = valid_unenrolled(identity, contract.get("boot_id")) and identity == receipt.get("initial_identity")
            if not configured and not unenrolled:
                raise DeployError("initialization recovery device identity differs")
            active_boot = current.get("current_boot_id", receipt["boot_id"])
            if contract.get("boot_id") != active_boot and current.get("state") not in (
                    "rolled-back", "rollback_pending", "rollback_incomplete"):
                raise DeployError("initialization boot changed")
            reboot_pending = current.get("reboot_required") and current.get("reboot_state") in (
                "required", "armed", "awaiting-resume", "resumed")
            if args.command == "confirm" and (not configured or contract.get("boot_id") != active_boot
                                               or not current.get("reload_complete") and not reboot_pending):
                raise DeployError("initialization is not ready for confirmation")
            return transport, current
        except DeployError as error:
            last_error = error
            transport.close()
    raise DeployError("initialization recovery failed: " + str(last_error))


def public_status(value):
    return {k: v for k, v in value.items() if k not in ("token", "baseline", "files", "modes", "agent")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest")
    sub = parser.add_subparsers(dest="command", required=True)
    for action in ("plan", "deploy", "verify", "status", "confirm", "rollback"):
        child = sub.add_parser(action)
        child.add_argument("--device")
        child.add_argument("--all", action="store_true")
        child.add_argument("--target-host", "--host", dest="target_host", metavar="[USER@]HOST",
                           help="Override the inferred target address (default user: root); --host is an alias")
        post = child.add_mutually_exclusive_group()
        post.add_argument("--post-switch-host", metavar="[USER@]HOST",
                          help="SSH destination after activation or reboot; preserves the declared management address")
        post.add_argument("--ask-post-switch-host", action="store_true",
                          help="Ask on the foreground terminal for the address after activation or reboot")
        child.add_argument("--port", type=int)
        child.add_argument("--post-switch-port", type=int, help="SSH port after activation or reboot (default: keep initial port)")
        child.add_argument("--control-socket")
        child.add_argument("--jump-host")
        child.add_argument("--known-hosts")
        child.add_argument("--initialize", "--init", action="store_true",
                           help="Allow first enrollment of one verified uninitialized device")
        child.add_argument("--quiet", "-q", action="store_true", help="Hide progress messages; errors remain visible")
        child.add_argument("--receipt")
        child.add_argument("--ttl", type=int)
        child.add_argument("--wait", type=int, default=240)
        child.add_argument("--remote", action="store_true", help="read remote state; plan never decrypts secrets")
        child.add_argument("--auto-confirm", action="store_true", help="confirm after configuration readback; required for bulk deploy")
    for action in ("receipts", "clean"):
        child = sub.add_parser(action)
        child.add_argument("--device", help="Filter saved receipts by device ID")
        child.add_argument("--quiet", "-q", action="store_true", help="Hide progress messages; errors remain visible")
        if action == "clean":
            child.add_argument("--older-than", type=int, default=30, metavar="DAYS",
                               help="Remove terminal receipts older than this many days (default: 30)")
    args = parser.parse_args(argv)
    progress = Progress("openwrt-deploy", quiet=args.quiet)
    if args.command in ("receipts", "clean"):
        if args.command == "clean" and args.older_than < 0:
            parser.error("--older-than must be a nonnegative number of days")
        label = "Listing saved transaction receipts" if args.command == "receipts" else "Cleaning saved transaction receipts"
        with progress.stage(label):
            result = {"directory": str(default_directory())}
            if args.command == "receipts":
                result["receipts"] = list_receipts(device=args.device)
            else:
                result.update(clean_receipts(device=args.device, older_than_days=args.older_than))
        emit(result)
        return
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.post_switch_port is not None and not 1 <= args.post_switch_port <= 65535:
        parser.error("--post-switch-port must be between 1 and 65535")
    if (args.post_switch_host is not None or args.post_switch_port is not None or args.ask_post_switch_host) and (args.all
            or args.command not in ("plan", "deploy", "confirm")
            or args.command in ("plan", "deploy") and not args.device
            or args.command == "confirm" and not args.receipt):
        parser.error("post-switch connection options require one device for plan/deploy, or receipt confirmation")
    if args.ask_post_switch_host and args.command != "plan" and not interactive_terminal():
        parser.error("--ask-post-switch-host requires a foreground controlling terminal; use --post-switch-host")
    if args.initialize and (args.command not in ("plan", "deploy") or args.receipt and args.command != "deploy"
                            or args.all or not args.device or not args.target_host
                            or args.command == "plan" and not args.remote):
        parser.error("--initialize requires one --device, --target-host and deploy or plan --remote")
    if args.command in ("confirm", "rollback", "status") and args.receipt:
        # Recovery must remain possible after editing config, rotating a cipher,
        # or losing the checkout. The target binds its token to the saved journal.
        with progress.stage("Loading transaction receipt"):
            receipt = read_json(args.receipt)
        if args.post_switch_host is not None:
            receipt["post_switch_host"] = args.post_switch_host
        if args.post_switch_port is not None:
            receipt["post_switch_port"] = args.post_switch_port
        if receipt.get("schema_version") != 1 or receipt.get("protocol") != "nixiz-v2" or not isinstance(receipt.get("device_id"), str):
            raise DeployError("invalid transaction receipt")
        if args.device and args.device != receipt["device_id"]:
            raise DeployError("receipt belongs to another device")
        if args.all:
            raise DeployError("a receipt addresses one device")
        args._receipt_port_override = args.port
        if args.port is None:
            args.port = receipt_port(receipt, "port", 22)
        device_progress = progress.for_device(receipt["device_id"])
        prompted_current = False
        if args.ask_post_switch_host and not args.target_host:
            compiled = {"target_host": receipt["host"], "ssh_port": args.port}
            initial = Transport(args, {"deployment": {}}, compiled, receipt["device_id"],
                                host=receipt.get("user", "root") + "@" + receipt["host"])
            deadline = time.monotonic() + min(args.wait, remaining_rollback_seconds(receipt))
            receipt["post_switch_host"] = prompt_post_switch_host(initial, receipt, deadline)
            args.target_host = receipt["post_switch_host"]
            private_write(args.receipt, canonical(receipt))
            prompted_current = True
        with contextlib.ExitStack() as transports:
            with device_progress.stage("Connecting to transaction"):
                if receipt.get("initialize") is True:
                    transport, current = initialization_recovery(args, receipt, transports)
                else:
                    compiled = {"target_host": receipt["host"], "ssh_port": args.port,
                                "identity": receipt.get("expected_identity", {}),
                                "contract_sha256": receipt.get("contract_sha256")}
                    last_error = None
                    for endpoint_index, (target, port) in enumerate(receipt_endpoints(args, receipt)):
                        if "@" not in target:
                            target = receipt.get("user", "root") + "@" + target
                        initial = Transport(args, {"deployment": {}}, compiled, receipt["device_id"], host=target)
                        transport = transports.enter_context(initial.reconnect(
                            target, host_key_alias=receipt.get("host_key_alias"), port=port))
                        try:
                            current = transport.call("status")
                        except DeployError as error:
                            if any(reason in str(error) for reason in ("SSH host key changed", "SSH host key verification failed", "invalid transaction response")):
                                raise
                            last_error = error
                            transport.close()
                            continue
                        transaction_status(current, receipt)
                        verify_reboot_fallback(args, endpoint_index, current)
                        break
                    else:
                        raise DeployError("transaction endpoint unavailable: " + str(last_error))
                transaction_status(current, receipt)
                if receipt.get("post_switch_host"):
                    receipt["post_switch_host"] = transport.reconnect(receipt["post_switch_host"]).destination()
            if args.command == "confirm" and (args.post_switch_host is not None or args.post_switch_port is not None or (
                    args.ask_post_switch_host and not prompted_current and not current.get("reboot_required"))):
                with device_progress.stage("Verifying the post-switch connection"):
                    deadline = time.monotonic() + min(args.wait, remaining_rollback_seconds(receipt))
                    if args.ask_post_switch_host:
                        receipt["post_switch_host"] = prompt_post_switch_host(transport, receipt, deadline)
                        private_write(args.receipt, canonical(receipt))
                    transport, current = wait_post_switch(transport, receipt, max(0, deadline - time.monotonic()), transports)
            if args.command == "confirm" and current.get("reboot_required") and current.get("state") != "confirmed":
                with device_progress.stage("Verifying the required target reboot"):
                    transport, current = complete_planned_reboot(
                        transport, receipt, args.receipt, current, transports, args.wait, device_progress,
                        ask_post_switch_host=args.ask_post_switch_host)
            label = {"confirm": "Confirming deployment", "rollback": "Requesting deployment rollback",
                     "status": "Reading transaction status"}[args.command]
            with device_progress.stage(label):
                result = transport.call(args.command, receipt["token"])
            if result.get("state") == "confirmed":
                device_progress.message("Deployment confirmed")
            elif args.command == "rollback":
                device_progress.message("Rollback requested; use status to check completion")
            elif result.get("state") == "applied" and result.get("reboot_state") == "awaiting-resume":
                device_progress.message("Target reboot awaits resume; use confirm with this receipt to continue, or rollback to restore")
            save_transaction_status(args.receipt, receipt, result, transport)
            emit(public_status(result))
            return
    with progress.stage("Loading deployment manifest"):
        manifest = load_manifest(args)
    selected = list(manifest["devices"]) if args.all else ([args.device] if args.device else [])
    if not selected or any(node not in manifest["devices"] for node in selected):
        raise DeployError("select --device NAME or --all")
    if len(selected) > 1 and (args.receipt or args.target_host or args.control_socket):
        raise DeployError("per-device connection or receipt arguments cannot be combined with --all")
    if args.command == "deploy" and len(selected) > 1 and not args.auto_confirm:
        raise DeployError("bulk deploy requires --auto-confirm; otherwise deploy and confirm one device at a time")
    runtime = args.command in ("deploy", "verify")
    resolver = SecretResolver.from_config(manifest.get("decryption", {}))
    transports = contextlib.ExitStack()
    try:
        # All inputs are resolved before first remote write, including fleet peers.
        label = "Resolving secrets and compiling configuration" if runtime else "Compiling configuration preview"
        with progress.stage(label):
            fleet = compile_fleet(manifest, resolve_secrets=runtime, resolver=resolver)
        for node in selected:
            device_progress = progress.for_device(node)
            compiled = fleet[node]
            transport = transports.enter_context(Transport(args, manifest, compiled, node))
            if args.post_switch_host is not None:
                transport.reconnect(args.post_switch_host)  # Parse without connecting or changing configuration.
            if args.command == "plan":
                result = {"device": node, "target_host": compiled["target_host"],
                          "managed_paths": sorted(compiled["files"]), "packages": compiled["packages"],
                          "secret_references": compiled["secret_references"], "secret_values_compared": False}
                if args.post_switch_host is not None:
                    result["post_switch_host"] = args.post_switch_host
                if args.post_switch_port is not None:
                    result["post_switch_port"] = args.post_switch_port
                if args.ask_post_switch_host:
                    result["ask_post_switch_host"] = True
                if args.remote:
                    with device_progress.stage("Checking agent identity"):
                        contract = transport.agent_preflight(allow_initialize=args.initialize)
                    result["remote_identity_verified"] = True
                    if args.initialize:
                        result["initialization_required"] = not contract["identity"].get("device_id")
                device_progress.message("Configuration preview ready")
                emit(result)
                continue
            if args.command in ("confirm", "rollback", "status"):
                if args.command == "status" and not args.receipt:
                    with device_progress.stage("Reading transaction status"):
                        result = transport.call("status")
                    emit(public_status(result))
                    continue
                if not args.receipt:
                    raise DeployError("transaction command requires --receipt")
                receipt = read_json(args.receipt)
                if (receipt.get("device_id") != node or receipt.get("host") != transport.host
                        or receipt.get("manifest_sha256") != digest(canonical(manifest))):
                    raise DeployError("receipt target differs from selected device")
                result = transport.call(args.command, receipt["token"])
                if result.get("token") != receipt["token"]:
                    raise DeployError("receipt transaction identity differs")
                receipt["state"] = result.get("state")
                private_write(args.receipt, canonical(receipt))
                emit(public_status(result))
                continue
            with device_progress.stage("Checking agent identity"):
                contract = transport.agent_preflight(allow_initialize=args.initialize)
            initialize = args.initialize and not contract["identity"].get("device_id")
            if compiled["packages"]:
                # Package installs/removals are deliberately separate from rollbackable config.
                probe = "\n".join("(apk info -e " + shlex.quote(package) + " >/dev/null 2>&1 || opkg status " + shlex.quote(package) + " 2>/dev/null | grep -q 'Status:.* installed') || exit 42" for package in compiled["packages"])
                with device_progress.stage("Checking installed packages"):
                    transport._run_stage("installed package preflight", probe)
            if args.command == "deploy":
                with device_progress.stage("Preparing deployment runtime"):
                    transport.ensure_engine()
            else:
                with device_progress.stage("Checking deployment runtime"):
                    if not transport.installed():
                        raise DeployError("target has no nixiz agent; install the package first")
            with device_progress.stage("Reading current configuration"):
                old = transport.ledger()
                if old and (initialize or old.get("device_id") != node):
                    raise DeployError("managed resource ledger belongs to another device or is present on an uninitialized target")
                request = {"paths": managed_paths(compiled, old),
                           "services": sorted(set(compiled["services"]) | set(old.get("services", [])))}
                baseline = transport.call("check", data=canonical(request))
                if initialize and (baseline.get("boot_id") != contract.get("boot_id")
                                   or baseline.get("identity") != contract["identity"]):
                    raise DeployError("initialization identity or boot changed before staging")
            changed = changed_files(compiled, baseline, old)
            service_changes = service_differences(compiled, baseline, old)
            agent = baseline.get("agent", {})
            root_ok = not compiled.get("root_password_managed") or agent.get("root_password_synced") is True
            resolver_ok = compiled.get("resolver_target") is None or baseline.get("resolver_state") == {
                "type": "symlink", "target": compiled["resolver_target"]}
            auxiliary_ok = root_ok and resolver_ok
            if args.command == "verify":
                with device_progress.stage("Verifying configuration"):
                    transport.call("verify", data=canonical({"checks": compiled["checks"]}))
                    emit({"device": node, "matches": not changed and not service_changes and auxiliary_ok,
                          "different_paths": changed, "different_services": service_changes,
                          "root_and_resolver_match": auxiliary_ok})
                    if changed or service_changes or not auxiliary_ok:
                        raise DeployError("target configuration differs from evaluated fleet")
                device_progress.message("Configuration matches")
                continue
            if (not changed and not service_changes and auxiliary_ok
                    and set(old.get("files", [])) == set(compiled["files"])
                    and set(old.get("services", [])) == set(compiled["services"])
                    and old.get("device_id") == node):
                transport.call("verify", data=canonical({"checks": compiled["checks"]}))
                device_progress.message("No changes required")
                emit({"device": node, "state": "noop", "changed_paths": [], "confirmation_required": False})
                continue
            device_progress.message("Configuration differs: " + str(len(changed)) + " files, " + str(len(service_changes)) + " services")
            if not auxiliary_ok:
                device_progress.message("Root credential or resolver state also needs synchronization")
            # A receipt contains only transaction capability and metadata, never plaintext.
            with device_progress.stage("Preparing rollback transaction"):
                receipt_path = args.receipt or str(allocate_receipt(node))
                if Path(receipt_path).exists() or Path(receipt_path).is_symlink():
                    raise DeployError("receipt exists; choose a new private receipt")
                ttl = args.ttl or manifest.get("deployment", {}).get("rollbackTimeout", 600)
                if not 60 <= ttl <= 7200:
                    raise DeployError("rollback timeout outside supported range")
                transaction, data = pending_bundle(compiled, baseline, node, ttl, old, initialize=initialize)
                receipt = {"schema_version": 1, "protocol": "nixiz-v2", "device_id": node, "host": transport.host,
                           "port": transport.port, "initial_port": transport.port, "initial_host": transport.host,
                           "user": transport.user, "token": transaction["token"], "state": "prepared",
                           "boot_id": baseline["boot_id"], "origin_boot_id": baseline["boot_id"],
                           "rollback_deadline": time.time() + ttl, "host_key_alias": transport.host_key_alias,
                           "changed_paths": changed,
                           "manifest_sha256": digest(canonical(manifest))}
                if args.post_switch_host is not None:
                    receipt["post_switch_host"] = transport.reconnect(args.post_switch_host).destination()
                if args.post_switch_port is not None:
                    receipt["post_switch_port"] = args.post_switch_port
                receipt.update({"expected_identity": compiled["identity"], "contract_sha256": compiled["contract_sha256"]})
                if initialize:
                    receipt.update({"initialize": True, "initial_host": transport.host,
                                    "management_host": compiled["target_host"], "initial_identity": baseline["identity"],
                                    "configured_ssh_port": compiled["ssh_port"]})
                private_write(receipt_path, canonical(receipt), replace=False)
            with device_progress.stage("Uploading configuration"):
                staged = transport.call("stage", data=data)
                if staged.get("token") != receipt["token"]:
                    raise DeployError("staged transaction identity differs")
                save_transaction_status(receipt_path, receipt, staged, transport)
            device_progress.message("Automatic rollback armed until confirmation")
            with device_progress.stage("Applying configuration and waiting for services"):
                try:
                    applied = transport.call("apply", receipt["token"])
                except DeployError:
                    device_progress.message("Apply response unavailable; checking transaction state")
                    if not initialize and not args.post_switch_host and args.post_switch_port is None and not args.ask_post_switch_host:
                        applied = wait_ready(transport, receipt["token"], min(args.wait, ttl - 15))
                    else:
                        applied = None
                if applied is not None and applied.get("token") != receipt["token"]:
                    raise DeployError("applied transaction identity differs")
                if args.ask_post_switch_host:
                    if applied is None:
                        try:
                            applied = transport.call("status")
                        except DeployError:
                            applied = None
                        else:
                            transaction_status(applied, receipt)
                    if applied is not None and applied.get("reboot_required"):
                        ready = wait_ready(transport, receipt["token"], min(args.wait, ttl - 15))
                    else:
                        deadline = time.monotonic() + min(args.wait, remaining_rollback_seconds(receipt))
                        receipt["post_switch_host"] = prompt_post_switch_host(transport, receipt, deadline)
                        private_write(receipt_path, canonical(receipt))
                        transport, ready = wait_post_switch(transport, receipt, max(0, deadline - time.monotonic()), transports)
                elif args.post_switch_host is not None or args.post_switch_port is not None:
                    transport, ready = wait_post_switch(
                        transport, receipt, min(args.wait, ttl - 15), transports)
                elif initialize:
                    # A declared resource change may require a protected boot.
                    # Enrollment itself does not impose a network topology.
                    initial_transport = transport
                    ready = wait_ready(transport, receipt["token"], min(args.wait, ttl - 15))
                    if not ready.get("reboot_required"):
                        device_progress.message("Reconnecting to the declared management address")
                        transport = transports.enter_context(transport.reconnect(compiled["target_host"], port=receipt_port(receipt, "post_switch_port", transport.port)))
                        ready = wait_initialized(transport, receipt, min(args.wait, ttl - 15),
                                                 initial_transport=initial_transport, receipt_path=receipt_path)
                    receipt.update({"host": transport.host, "user": transport.user, "port": transport.port})
                    private_write(receipt_path, canonical(receipt))
                else:
                    ready = wait_ready(transport, receipt["token"], min(args.wait, ttl - 15))
            if ready.get("reboot_required"):
                with device_progress.stage("Verifying the required target reboot"):
                    transport, ready = complete_planned_reboot(
                        transport, receipt, receipt_path, ready, transports, args.wait, device_progress,
                        ask_post_switch_host=args.ask_post_switch_host)
            if args.auto_confirm:
                with device_progress.stage("Checking applied configuration"):
                    actual = transport.call("check", data=canonical(request))
                    if changed_files(compiled, actual, transport.ledger()) or service_differences(compiled, actual, transport.ledger()):
                        raise DeployError("automatic confirmation refused: readback differs")
                with device_progress.stage("Confirming deployment"):
                    ready = transport.call("confirm", receipt["token"])
            save_transaction_status(receipt_path, receipt, ready, transport)
            if ready["state"] == "confirmed":
                device_progress.message("Deployment confirmed")
            else:
                device_progress.message("Deployment applied; confirmation required")
            emit({"device": node, "state": ready["state"], "reload_complete": True,
                  "changed_paths": changed, "receipt": receipt_path, "confirmation_required": not args.auto_confirm})
    finally:
        transports.close()
        resolver.close()


if __name__ == "__main__":
    try:
        main()
    except (CompileError, SecretError, DeployError, ReceiptError) as error:
        print("openwrt-deploy: " + str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print("openwrt-deploy: operation failed; details suppressed to protect secrets", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
