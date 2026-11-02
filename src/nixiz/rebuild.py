#!/usr/bin/env python3
# This file was generated entirely by AI.
# It has not been reviewed line by line by a human.
"""Build and apply declarative OpenWrt configuration with guarded transactions."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys

from openwrt import DeployError
from progress import Progress

HERE = Path(__file__).resolve().parent
DEFAULT_FLAKE = Path("/etc/nixos/flake.nix")
ACTIONS = ("build", "dry-run", "test", "switch", "verify", "status", "confirm", "rollback", "receipts", "clean")
CONNECTION = ("target_host", "post_switch_host", "post_switch_port", "port", "control_socket", "jump_host", "known_hosts")


class RebuildError(ValueError):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="openwrt-rebuild", description=__doc__, allow_abbrev=False, epilog=(
        "build prepares configuration/tools, not a firmware image. test keeps changes in a pending "
        "transaction; noop needs no receipt. Confirm changed configurations to keep them. switch confirms after configuration "
        "readback. switch --receipt confirms a previous test without rebuilding."))
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--flake", nargs="?", const=".", metavar="FLAKE[#HOSTNAME]",
                        help="Select a NixOS or standalone configuration (default: local hostname); "
                             "bare --flake uses the current directory, omitted --flake uses /etc/nixos/flake.nix")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--device")
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--out-link", default=None, help="Nix build output link (default: ./result)")
    parser.add_argument("--offline", action="store_true", help="Use only already available Nix inputs")
    parser.add_argument("--quiet", "-q", action="store_true", help="Hide progress messages; errors remain visible")
    parser.add_argument("--print-build-logs", "-L", action="store_true", help="Print Nix build logs")
    parser.add_argument("--receipt", help="Private transaction receipt; recovery never evaluates the flake")
    parser.add_argument("--older-than", type=int, metavar="DAYS",
                        help="For clean: remove completed receipts older than DAYS (default: 30)")
    parser.add_argument("--target-host", "--host", dest="target_host", metavar="[USER@]HOST",
                        help="Override the device SSH destination (default: declared management address, root user)")
    post = parser.add_mutually_exclusive_group()
    post.add_argument("--post-switch-host", metavar="[USER@]HOST",
                      help="SSH destination after activation or reboot; does not change the declared management address")
    post.add_argument("--ask-post-switch-host", action="store_true",
                      help="Ask on the controlling terminal for the actual DHCP address after activation or reboot")
    parser.add_argument("--port", type=int)
    parser.add_argument("--post-switch-port", type=int, help="SSH port after activation or reboot (default: keep initial port)")
    parser.add_argument("--control-socket", help="Reuse a caller-owned SSH master socket")
    parser.add_argument("--jump-host")
    parser.add_argument("--known-hosts")
    parser.add_argument("--ttl", type=int, help="Rollback confirmation window in seconds")
    parser.add_argument("--wait", type=int, help="Time to wait for activation")
    parser.add_argument("--remote", action="store_true", help="Also check remote identity during dry-run")
    parser.add_argument("--initialize", "--init", dest="initialize", action="store_true",
                        help="Allow first provisioning of one explicitly selected, unenrolled device")
    args = parser.parse_args(argv)
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.post_switch_port is not None and not 1 <= args.post_switch_port <= 65535:
        parser.error("--post-switch-port must be between 1 and 65535")
    if args.older_than is not None and (args.action != "clean" or args.older_than < 0):
        parser.error("--older-than requires clean and a nonnegative number of days")
    if args.action in ("receipts", "clean"):
        if (args.all or args.receipt is not None or args.remote or args.offline or args.initialize or args.ask_post_switch_host
                or args.print_build_logs or args.ttl is not None or args.wait is not None
                or args.out_link is not None or any(getattr(args, field) is not None for field in CONNECTION)):
            parser.error(args.action + " only accepts --device, --quiet and, for clean, --older-than")
        return args, False
    receipt_mode = bool(args.receipt) and args.action in ("confirm", "rollback", "status", "switch")
    if (args.post_switch_host is not None or args.post_switch_port is not None or args.ask_post_switch_host) and args.action not in ("test", "switch", "dry-run", "confirm"):
        parser.error("post-switch connection options apply only to test, switch, dry-run or receipt confirmation")
    if args.initialize:
        if args.action not in ("test", "switch", "dry-run") or (args.action == "dry-run" and not args.remote):
            parser.error("--initialize requires test, switch or dry-run --remote")
        if not args.device or args.all or not args.target_host:
            parser.error("--initialize requires one --device and an explicit --target-host")
    if args.action in ("confirm", "rollback") and not args.receipt:
        parser.error(args.action + " requires --receipt")
    if receipt_mode:
        if (args.all or args.remote or args.initialize or args.print_build_logs or args.ttl is not None
                or args.wait is not None or args.out_link is not None):
            parser.error("receipt recovery cannot use --all, --remote, --initialize, --print-build-logs, --ttl, --wait or --out-link")
        return args, True
    if args.action == "build":
        if (args.device or args.all or args.receipt or args.remote or args.ttl is not None
                or args.wait is not None or any(getattr(args, field) is not None for field in CONNECTION)):
            parser.error("build prepares the configuration bundle; select devices and connection options when applying it")
    elif not args.device and not args.all:
        parser.error("select --device NAME or --all")
    if args.all and (args.ask_post_switch_host or any(getattr(args, field) is not None for field in ("target_host", "post_switch_host", "post_switch_port", "control_socket"))):
        parser.error("per-device connection options require one --device")
    if args.action == "test" and args.all:
        parser.error("test requires one --device; each device has its own confirmation and rollback")
    if args.remote and args.action != "dry-run":
        parser.error("--remote is only meaningful with dry-run")
    if args.receipt and args.action != "test":
        parser.error("--receipt stores a test transaction or addresses an existing transaction")
    if (args.ttl is not None or args.wait is not None) and args.action not in ("test", "switch"):
        parser.error("--ttl and --wait apply only to test or switch")
    if args.ttl is not None and not 60 <= args.ttl <= 7200:
        parser.error("--ttl must be between 60 and 7200 seconds")
    if args.wait is not None and args.wait < 1:
        parser.error("--wait must be positive")
    return args, False


def flake_target(args):
    if args.flake is None:
        if not DEFAULT_FLAKE.is_file():
            raise RebuildError("no default flake at /etc/nixos/flake.nix; "
                               "use --flake for the current directory or --flake FLAKE[#HOSTNAME]")
        # Match nixos-rebuild: a symlinked flake selects its source directory.
        base, name = str(DEFAULT_FLAKE.resolve().parent), ""
    else:
        base, _, name = args.flake.partition("#")
        base = base or "."
    name = name or socket.gethostname()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name.startswith("-"):
        raise RebuildError("select a valid configuration name")
    return base, name


def nix_json(words):
    # Closing child stdin prevents Nix from consuming a caller's shell script.
    result = subprocess.run(["nix", *words], input=b"", stdout=subprocess.PIPE, check=False)
    if result.returncode:
        code = result.returncode if result.returncode > 0 else 128 - result.returncode
        raise RebuildError("Nix evaluation or build failed", code)
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise RebuildError("Nix returned invalid structured output") from None


def nix_string(value):
    return json.dumps(value).replace("${", "\\${")


def configuration_target(flake, name, options):
    for namespace in ("nixosConfigurations", "openwrtConfigurations"):
        prefix = namespace + "."
        if name.startswith(prefix):
            selected = name[len(prefix):]
            if not selected:
                raise RebuildError("explicit configuration selection needs a name")
            return flake + "#" + namespace + "." + json.dumps(selected) + ".config"
    # Inspect output membership without evaluating whole NixOS configurations.
    # flake metadata resolves relative paths and registries; getFlake receives
    # the resolved URL as a quoted literal, never executable user expression.
    metadata = nix_json(["flake", "metadata", "--json", *options, flake])
    if not isinstance(metadata, dict) or not isinstance(metadata.get("url"), str):
        raise RebuildError("Nix returned no resolved flake URL")
    expression = ("let f = builtins.getFlake " + nix_string(metadata["url"]) + "; in { "
        + "nixos = builtins.hasAttr " + nix_string(name) + " (f.nixosConfigurations or {}); "
        + "standalone = builtins.hasAttr " + nix_string(name) + " (f.openwrtConfigurations or {}); }")
    found = nix_json(["eval", "--json", "--impure", *options, "--expr", expression])
    if found == {"nixos": True, "standalone": True}:
        raise RebuildError("configuration name is ambiguous; use #nixosConfigurations." + name
                           + " or #openwrtConfigurations." + name)
    if found == {"nixos": True, "standalone": False}:
        namespace = "nixosConfigurations"
    elif found == {"nixos": False, "standalone": True}:
        namespace = "openwrtConfigurations"
    else:
        raise RebuildError("configuration was not found in nixosConfigurations or openwrtConfigurations")
    return flake + "#" + namespace + "." + json.dumps(name) + ".config"


def build_configuration(args):
    flake, name = flake_target(args)
    options = ["--no-write-lock-file"] + (["--offline"] if args.offline else [])
    target = configuration_target(flake, name, options)
    progress = Progress("openwrt-rebuild", args.quiet)
    with progress.stage("Evaluating OpenWrt configuration"):
        checks = nix_json(["eval", "--json", *options, target, "--apply",
                           "c: { enabled = c.services.openwrt.enable; "
                           "failed = builtins.length (builtins.filter (a: !a.assertion) (c.assertions or [])); }"])
        if not isinstance(checks, dict) or checks.get("enabled") is not True or type(checks.get("failed")) is not int or checks.get("failed") != 0:
            raise RebuildError("OpenWrt must be enabled and all host configuration assertions must pass")
    with progress.stage("Building deployment tools", animate=not args.print_build_logs):
        build_options = ["--print-build-logs"] if args.print_build_logs else []
        built = nix_json(["build", "--json", *options, *build_options, "--out-link", args.out_link or "result",
                          target + ".system.build.openwrt-deploy"])
        try:
            output = built[0]["outputs"]["out"]
            valid = len(built) == 1 and isinstance(output, str) and Path(output).is_absolute()
        except (KeyError, IndexError, TypeError):
            valid = False
        if not valid:
            raise RebuildError("Nix did not return one deployment bundle")
        command = Path(output) / "bin/openwrt-deploy"
        if not command.is_file() or not os.access(command, os.X_OK):
            raise RebuildError("built deployment bundle has no executable openwrt-deploy")
    return Path(output), command


def controller_args(args, action):
    words = [action]
    if args.device:
        words += ["--device", args.device]
    if args.all:
        words.append("--all")
    for field in (*CONNECTION, "receipt", "ttl", "wait"):
        value = getattr(args, field)
        if value is not None:
            words += ["--" + field.replace("_", "-"), str(value)]
    if args.remote:
        words.append("--remote")
    if args.initialize:
        words.append("--initialize")
    if args.ask_post_switch_host:
        words.append("--ask-post-switch-host")
    if args.quiet:
        words.append("--quiet")
    if action == "clean":
        words += ["--older-than", str(args.older_than if args.older_than is not None else 30)]
    if args.action == "switch" and not args.receipt:
        words.append("--auto-confirm")
    return words


def main(argv=None):
    args, receipt_mode = parse_args(argv)
    progress = Progress("openwrt-rebuild", args.quiet)
    if args.action in ("receipts", "clean"):
        progress.message("Listing locally stored receipts" if args.action == "receipts" else "Cleaning completed local receipts")
        os.execv(sys.executable, [sys.executable, str(HERE / "openwrt.py"), *controller_args(args, args.action)])
        return
    if receipt_mode:
        action = "confirm" if args.action == "switch" else args.action
        progress.message("Recovering transaction from receipt")
        os.execv(sys.executable, [sys.executable, str(HERE / "openwrt.py"), *controller_args(args, action)])
        return
    output, command = build_configuration(args)
    if args.action == "build":
        print(output)
        return
    action = {"dry-run": "plan", "test": "deploy", "switch": "deploy"}.get(args.action, args.action)
    if args.action == "test":
        print("openwrt-rebuild: changed configurations stay unconfirmed; receipts are saved in local state by default. Confirm or roll back before TTL expires. noop creates no new receipt", file=sys.stderr)
    progress.message("Starting configuration operation")
    os.execv(str(command), [str(command), *controller_args(args, action)])


if __name__ == "__main__":
    try:
        main()
    except RebuildError as error:
        print("openwrt-rebuild: " + str(error), file=sys.stderr)
        sys.exit(error.code)
    except DeployError as error:
        print("openwrt-rebuild: " + str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        print("openwrt-rebuild: operation failed; no new confirmation was performed", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
