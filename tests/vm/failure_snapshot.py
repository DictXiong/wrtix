#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Bounded, read-only diagnostics for an already failed disposable VM."""
from __future__ import annotations
from contextlib import contextmanager
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import time

_PREFIX = "WRTIXSNAP "
_BOOT = re.compile(rb"SeaBIOS|Linux version [^\r\n]+|reboot: (?:Restarting system|machine restart)")
_PROMPT = re.compile(rb"(?:^|[\r\n])root@OpenWrt:[^\r\n]{0,128}# ?")
_PROCESSES = {"procd", "netifd", "dropbear", "flock", "jq", "kmodloader",
              "rcS", "rc.common", "S03wrtix-agent", "wrtix-agent", "agent", "sh", "ubus", "timeout"}
_STATES = {"applying", "absent", "staged", "applied", "confirmed", "rolled-back",
           "rollback_pending", "rollback_incomplete", "reload_failed"}
_PHASES = {"none", "required", "armed", "awaiting-resume", "resumed"}
_QMP_STATES = {"running", "paused", "prelaunch", "shutdown", "suspended",
               "guest-panicked", "internal-error", "io-error", "watchdog",
               "inmigrate", "postmigrate", "finish-migrate", "restore-vm",
               "save-vm", "debug", "colo"}

# Output is parsed by an allowlist below. Never return the terminal transcript:
# even an unrelated process argument could contain a transaction credential.
_SCRIPT = r"""
printf '\nWRTIXSNAP uptime '; cut -d. -f1 /proc/uptime
printf 'WRTIXSNAP boot_id '; cat /proc/sys/kernel/random/boot_id
printf 'WRTIXSNAP mac '; cat /sys/class/net/eth0/address
printf 'WRTIXSNAP carrier '; cat /sys/class/net/eth0/carrier
for family in 4 6; do
 if routes=$(ip -$family route show dev eth0 2>/dev/null); then
  printf '%s\n' "$routes" | while IFS= read -r route; do [ -z "$route" ] || printf 'WRTIXSNAP route%s %s\n' "$family" "$route"; done
  printf 'WRTIXSNAP route%s_scan true\n' "$family"
 fi
done
printf 'WRTIXSNAP state_dir_scan true\n'
if [ -d /etc/wrtix/transactions/state ]; then
 printf 'WRTIXSNAP state_dir_present true\n'
 for marker in candidate-reboot-issued reboot.json reboot-required applying applied confirmed rolled-back reload-complete restore-pending rollback-incomplete rollback-reboot; do
  if [ -e "/etc/wrtix/transactions/state/$marker" ]; then printf 'WRTIXSNAP marker %s true\n' "$marker"; else printf 'WRTIXSNAP marker %s false\n' "$marker"; fi
 done
else printf 'WRTIXSNAP state_dir_present false\n'; fi
if addresses=$(ip -o addr show dev eth0 2>/dev/null); then
 printf '%s\n' "$addresses" | awk '$3=="inet"||$3=="inet6"{print "WRTIXSNAP address "$4}'
 printf 'WRTIXSNAP addresses_scan true\n'
fi
if [ -r /proc/net/tcp ] && [ -r /proc/net/tcp6 ]; then
 if listen=$(awk '$4=="0A"&&$2~/:0016$/{yes=1}END{print (yes?"true":"false")}' /proc/net/tcp /proc/net/tcp6); then
  printf 'WRTIXSNAP ssh_scan true\nWRTIXSNAP ssh_listen %s\n' "$listen"
 fi
fi
process_scan=true; process_count=0
for p in /proc/[0-9]*; do
 read -r n < "$p/comm" 2>/dev/null || continue
 case "$n" in procd|netifd|dropbear|flock|jq|kmodloader|rcS|rc.common|S03wrtix-agent|wrtix-agent|agent|sh|ubus|timeout)
  process_count=$((process_count + 1))
  if [ "$process_count" -gt 64 ]; then process_scan=false; break; fi
  pid=${p##*/}; state=; wait=
  while read -r label value rest; do [ "$label" != State: ] || { state=$value; break; }; done < "$p/status" 2>/dev/null
  read -r wait < "$p/wchan" 2>/dev/null || :
  if [ -n "$state" ] && [ -n "$wait" ]; then
   printf 'WRTIXSNAP process %s %s %s %s\n' "$pid" "$n" "$state" "$wait"
  else process_scan=false; fi;;
 esac
done
[ "$process_scan" != true ] || printf 'WRTIXSNAP process_scan true\n'
if [ -r /proc/locks ] && inode=$(stat -c %i /etc/wrtix/transaction.lock 2>/dev/null) && [ -n "$inode" ]; then
 if locks=$(awk -v i="$inode" '$2=="FLOCK"&&$4=="WRITE"{split($6,a,":");if(a[3]==i)print "WRTIXSNAP lock_owner "$5}' /proc/locks); then
  printf '%s\n' "$locks"
  printf 'WRTIXSNAP lock_scan true\n'
 fi
fi
timeout 1 /usr/libexec/wrtix/agent status 2>/dev/null | jq -r '"WRTIXSNAP agent_state \(.state)\nWRTIXSNAP agent_phase \(.reboot_state)\nWRTIXSNAP reload_complete \(.reload_complete)"' 2>/dev/null
printf 'WRTIXSNAP end_boot_id '; cat /proc/sys/kernel/random/boot_id
printf 'WRTIXSNAP done\n'
"""


def _timeout(connection, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    connection.settimeout(remaining)


def _line(connection, buffer, deadline):
    while b"\n" not in buffer:
        _timeout(connection, deadline)
        chunk = connection.recv(4096)
        if not chunk:
            raise EOFError()
        buffer.extend(chunk)
        if len(buffer) > 65536:
            raise ValueError()
    line, _, tail = buffer.partition(b"\n")
    buffer[:] = tail
    return json.loads(line)


def _reply(connection, buffer, deadline, expected):
    for _ in range(256):
        response = _line(connection, buffer, deadline)
        if response.get("id") == expected:
            if "error" in response or "return" not in response:
                raise ValueError()
            return response["return"]
    raise ValueError()


def _qmp(directory, deadline):
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            _timeout(connection, deadline)
            connection.connect(str(directory / "monitor.sock"))
            buffer = bytearray()
            if "QMP" not in _line(connection, buffer, deadline):
                raise ValueError()
            for command, ident in (("qmp_capabilities", "cap"), ("query-status", "state")):
                _timeout(connection, deadline)
                connection.sendall(json.dumps({"execute": command, "id": ident}).encode() + b"\n")
                response = _reply(connection, buffer, deadline, ident)
            if response.get("status") not in _QMP_STATES or type(response.get("running")) is not bool:
                raise ValueError()
            return {"available": True, "status": response["status"], "running": response["running"]}
    except (TimeoutError, socket.timeout):
        return {"available": False, "reason": "timeout"}
    except (OSError, EOFError, ValueError, TypeError, AttributeError):
        return {"available": False, "reason": "channel_unavailable_or_invalid"}


_MILESTONES = {
    "restart": re.compile(rb"reboot: (?:Restarting system|machine restart)"),
    "firmware": re.compile(rb"SeaBIOS"),
    "linux": re.compile(rb"Linux version [^\r\n]+"),
    "procd_early": re.compile(rb"procd: - early -"),
    "procd_init": re.compile(rb"procd: - init -"),
    "rng_started": re.compile(rb"urngd: [^\r\n]{0,96}started\.?"),
    "normal_console": re.compile(rb"Please press Enter to activate this console\."),
    "failsafe": re.compile(rb"(?:^|[\r\n])(?:\[[^\]\r\n]{1,40}\]\s*)?[=* -]{0,32}(?:Entering\s+failsafe\b|FAILSAFE MODE\b|failsafe mode active\b)", re.I),
    "kernel_panic": re.compile(rb"Kernel panic - not syncing:"),
}
_MARKERS = {"candidate-reboot-issued", "reboot.json", "reboot-required", "applying",
            "applied", "confirmed", "rolled-back", "reload-complete", "restore-pending",
            "rollback-incomplete", "rollback-reboot"}


def _boot_evidence(data, source, offset=0):
    """Passive bounded bytes become fixed milestones, never arbitrary log lines."""
    matches = list(_BOOT.finditer(data))
    marker = matches[-1] if matches else None
    start = marker.start() if marker else 0
    tail = data[start:]
    milestones = {}
    for name, pattern in _MILESTONES.items():
        found = list(pattern.finditer(tail))
        if found:
            milestones[name] = {"count": len(found), "first_offset": offset + start + found[0].start(),
                                "last_offset": offset + start + found[-1].start()}
    kind = None
    if marker:
        raw = marker.group()
        kind = "linux" if raw.startswith(b"Linux") else "firmware" if raw.startswith(b"SeaBIOS") else "restart"
    if not marker:
        classification = "no_boot_evidence"
    elif "failsafe" in milestones:
        classification = "failsafe_evidence"
    elif "kernel_panic" in milestones:
        classification = "panic_evidence"
    elif kind == "linux" and "normal_console" in milestones:
        classification = "normal_console_evidence"
    elif kind == "linux" and "procd_init" in milestones and "rng_started" in milestones:
        classification = "normal_userspace_evidence"
    else:
        classification = "boot_evidence_only"
    return {"source": source, "scanned_offset": offset, "scanned_bytes": len(data),
            "boot_marker_count_in_window": len(matches),
            "latest_boot_marker": {"kind": kind, "offset": offset + start} if marker else None,
            "classification": classification, "milestones_since_latest_marker": milestones}


def _console_evidence(directory):
    path = directory / "console.log"
    if not path.is_file() or path.is_symlink():
        return {"source": "console.log", "available": False, "classification": "unavailable"}
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            length = stream.tell()
            offset = max(0, length - 131072)
            stream.seek(offset)
            data = stream.read(131072)
        result = _boot_evidence(data, "console.log", offset)
        result.update({"available": True, "file_bytes": length, "window_truncated": offset > 0})
        return result
    except OSError:
        return {"source": "console.log", "available": False, "classification": "unavailable"}


def _route(values, version):
    if not values:
        return None
    try:
        destination = str(ipaddress.ip_network(("0.0.0.0/0" if version == 4 else "::/0")
                          if values[0] == "default" else values[0], strict=False))
        if ipaddress.ip_network(destination).version != version:
            return None
        result = {"destination": destination}
        for name, target in (("via", "gateway"), ("src", "source")):
            if name in values:
                value = str(ipaddress.ip_address(values[values.index(name) + 1]))
                if ipaddress.ip_address(value).version != version:
                    return None
                result[target] = value
        if "metric" in values:
            value = values[values.index("metric") + 1]
            if not re.fullmatch(r"\d{1,10}", value):
                return None
            result["metric"] = int(value)
        if "dev" in values:
            value = values[values.index("dev") + 1]
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", value):
                return None
            result["device"] = value
        return result
    except (ValueError, IndexError):
        return None


def _serial_fields(transcript):
    fields = {}
    lists = {"addresses": [], "processes": [], "lock_owners": [], "routes4": [], "routes6": []}
    for raw in transcript.decode("utf-8", "replace").replace("\r", "").splitlines():
        if not raw.startswith(_PREFIX):
            continue
        parts = raw[len(_PREFIX):].split()
        if not parts:
            continue
        key, values = parts[0], parts[1:]
        if key == "uptime" and len(values) == 1 and re.fullmatch(r"\d{1,10}", values[0]):
            fields["uptime_seconds"] = int(values[0])
        elif key in ("boot_id", "end_boot_id") and len(values) == 1 and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", values[0]):
            fields[key] = values[0]
        elif key == "mac" and len(values) == 1 and re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", values[0]):
            fields["mac"] = values[0].lower()
        elif key == "carrier" and values in (["0"], ["1"]):
            fields["carrier"] = values[0] == "1"
        elif key == "marker" and len(values) == 2 and values[0] in _MARKERS and values[1] in ("true", "false"):
            fields.setdefault("guard_markers", {})[values[0]] = values[1] == "true"
        elif key in ("route4", "route6") and len(lists["routes" + key[-1]]) < 32:
            route = _route(values, int(key[-1]))
            if route is not None and route not in lists["routes" + key[-1]]:
                lists["routes" + key[-1]].append(route)
            elif route is None:
                fields["route" + key[-1] + "_parse_incomplete"] = True
        elif key == "address" and len(values) == 1 and len(values[0]) <= 64:
            try:
                address = str(ipaddress.ip_interface(values[0]))
                if address not in lists["addresses"] and len(lists["addresses"]) < 16:
                    lists["addresses"].append(address)
            except ValueError:
                pass
        elif key in ("ssh_listen", "addresses_scan", "ssh_scan", "process_scan", "lock_scan", "reload_complete", "route4_scan", "route6_scan", "state_dir_scan", "state_dir_present") and values in (["true"], ["false"]):
            fields[key] = values[0] == "true"
        elif key == "agent_state" and len(values) == 1 and values[0] in _STATES:
            fields[key] = values[0]
        elif key == "agent_phase" and len(values) == 1 and values[0] in _PHASES:
            fields[key] = values[0]
        elif key == "lock_owner" and len(values) == 1 and re.fullmatch(r"\d{1,8}", values[0]):
            if len(lists["lock_owners"]) < 16:
                lists["lock_owners"].append(int(values[0]))
        elif key == "process" and len(values) == 4 and len(lists["processes"]) < 64:
            pid, name, state, wait = values
            if (re.fullmatch(r"\d{1,8}", pid) and name in _PROCESSES
                    and re.fullmatch(r"[RSDZTWXIPtK]", state)
                    and re.fullmatch(r"[A-Za-z0-9_]{1,64}", wait)):
                lists["processes"].append({"pid": int(pid), "comm": name, "state": state, "wchan": wait})
        elif key == "done" and not values:
            fields["complete"] = True
    for name, marker in (("addresses", "addresses_scan"), ("processes", "process_scan"), ("lock_owners", "lock_scan"), ("routes4", "route4_scan"), ("routes6", "route6_scan")):
        if lists[name] or fields.get(marker) is True:
            fields[name] = lists[name]
    if fields.get("ssh_scan") is not True:
        fields.pop("ssh_listen", None)
    if "lock_owners" in fields:
        fields["lock_identity_scope"] = "inode_candidates_only"
    if fields.get("boot_id") and fields.get("end_boot_id"):
        fields["script_boot_consistent"] = fields["boot_id"] == fields["end_boot_id"]
    return fields


class _ConsoleChanged(Exception):
    pass


def _serial(directory, deadline):
    # Connect passively first. Missing log text never authorizes console input.
    passive, transcript, responses = bytearray(), bytearray(), bytearray()
    connected_file = _console_evidence(directory)
    stage, input_file_marker = connected_file["classification"], None
    connected_file_marker = connected_file.get("latest_boot_marker")
    sent_input = False
    def gate():
        file = _console_evidence(directory)
        stream = _boot_evidence(passive, "serial_stream")
        # Neither source may override an unsafe/new-epoch observation from
        # the other. Fresh stream boot markers cannot be aligned to file offsets
        # safely, so they remain passive evidence rather than input authority.
        if file["classification"] in ("failsafe_evidence", "panic_evidence"):
            return file["classification"], None
        if file.get("latest_boot_marker") != connected_file_marker:
            return "file_boot_epoch_changed", None
        if any(name in stream["milestones_since_latest_marker"] for name in ("failsafe", "kernel_panic")):
            return "unsafe_stream_evidence", None
        if stream["latest_boot_marker"] is not None:
            return "unaligned_stream_boot_evidence", None
        marker = file.get("latest_boot_marker")
        return file["classification"], ("file", marker["offset"]) if marker else None
    def unchanged():
        current = _console_evidence(directory)
        return (current.get("latest_boot_marker") == input_file_marker
                and current["classification"] in ("normal_console_evidence", "normal_userspace_evidence"))
    def safe_receive(connection, predicate):
        data = bytearray()
        while not predicate(data):
            _timeout(connection, deadline)
            chunk = connection.recv(4096)
            if not chunk:
                raise EOFError()
            data.extend(chunk)
            responses.extend(chunk)
            if len(data) > 65536 or len(responses) > 65536:
                raise ValueError()
            if (_BOOT.search(data) or _MILESTONES["failsafe"].search(data)
                    or _MILESTONES["kernel_panic"].search(data) or not unchanged()):
                raise _ConsoleChanged()
        return bytes(data)
    def unbound(reason):
        return {"available": False, "reason": reason, "gate": stage,
                "input_sent": sent_input, "guest_fields_discarded": sent_input,
                "passive_stream": _boot_evidence(passive, "serial_stream"),
                "response_evidence": _boot_evidence(responses, "serial_response")}
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            _timeout(connection, deadline)
            connection.connect(str(directory / "serial.sock"))
            while True:
                # Drain pending/current output before relying on old file bytes.
                _timeout(connection, min(deadline, time.monotonic() + .1))
                try:
                    chunk = connection.recv(4096)
                    if not chunk:
                        raise EOFError()
                    passive.extend(chunk)
                    if len(passive) > 65536:
                        raise ValueError()
                    continue
                except socket.timeout:
                    pass
                stage, identity = gate()
                if stage in ("failsafe_evidence", "panic_evidence", "unsafe_stream_evidence"):
                    return unbound("unsafe_console_window")
                if stage in ("normal_console_evidence", "normal_userspace_evidence"):
                    input_file_marker = _console_evidence(directory).get("latest_boot_marker")
                    if gate() != (stage, identity):
                        return unbound("boot_changed")
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError()
            _timeout(connection, deadline)
            connection.sendall(b"\n")
            sent_input = True
            safe_receive(connection, lambda data: bool(_PROMPT.search(data)))
            if gate() != (stage, identity) or not unchanged():
                return unbound("boot_changed")
            command = ("timeout 3 sh -c " + shlex.quote(_SCRIPT) + " 2>/dev/null\n").encode()
            if len(command) > 4000:
                raise ValueError()
            _timeout(connection, deadline)
            connection.sendall(command)
            transcript.extend(safe_receive(connection, lambda data: b"\nWRTIXSNAP done" in data.replace(b"\r", b"")))
            if gate() != (stage, identity) or not unchanged():
                return unbound("boot_changed")
            fields = _serial_fields(transcript)
            if fields.get("script_boot_consistent") is not True:
                return unbound("guest_boot_unbound")
            return {"available": True, "gate": stage, "input_sent": True, "fields": fields,
                    "passive_stream": _boot_evidence(passive, "serial_stream"),
                    "response_evidence": _boot_evidence(responses, "serial_response")}
    except _ConsoleChanged:
        return unbound("boot_changed_or_unsafe_stream")
    except (TimeoutError, socket.timeout):
        # A partial command or a racing boot must not masquerade as current state.
        return unbound("timeout" if not sent_input or unchanged() else "boot_changed")
    except (OSError, EOFError, ValueError, TypeError):
        return unbound("channel_unavailable_or_invalid" if not sent_input or unchanged() else "boot_changed")


def _fixture_info(directory):
    try:
        path = directory / "machine.json"
        if not path.is_file() or path.is_symlink():
            raise ValueError()
        info = json.loads(path.read_text())
        if (type(info.get("pid")) is not int or not 1 < info["pid"] < 1_000_000_000
                or not re.fullmatch(r"\d{1,20}", str(info.get("start_ticks", "")))
                or any(type(info.get(k)) is not int or not 1024 <= info[k] <= 65535
                       for k in ("port", "alternate_port"))):
            raise ValueError()
        return info
    except (OSError, ValueError, TypeError):
        return None


def _host_process(directory):
    info = _fixture_info(directory)
    if info is None:
        return {"available": False, "reason": "fixture_identity_unavailable"}
    try:
        base = Path("/proc") / str(info["pid"])
        fields = (base / "stat").read_text().rpartition(") ")[2].split()
        if len(fields) < 20 or fields[19] != str(info["start_ticks"]):
            return {"available": False, "reason": "owned_process_absent_or_reused"}
        threads = []
        for path in sorted((base / "task").iterdir())[:32]:
            try:
                name = (path / "comm").read_text().strip()
                if not re.fullmatch(r"CPU \d{1,3}/TCG", name):
                    continue
                stat = (path / "stat").read_text().rpartition(") ")[2].split()
                wait = (path / "wchan").read_text().strip()
                if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", wait):
                    wait = "unavailable"
                threads.append({"vcpu": int(name.split()[1].split("/")[0]), "state": stat[0],
                                "cpu_ticks": int(stat[11]) + int(stat[12]), "wchan": wait})
            except (OSError, ValueError, IndexError):
                continue
        return {"available": True, "identity": "pid_and_start_ticks_match", "state": fields[0],
                "cpu_ticks": int(fields[11]) + int(fields[12]), "TCG_threads": threads}
    except (OSError, ValueError, IndexError):
        return {"available": False, "reason": "owned_process_observation_unavailable"}


def _hostfwd(directory, deadline):
    info = _fixture_info(directory)
    if info is None:
        return {"available": False, "reason": "fixture_identity_unavailable"}
    observations = {}
    for name, host, port in (("primary_15", "127.0.0.1", info["port"]),
                             ("alternate_16", "127.0.0.1", info["alternate_port"]),
                             ("alias_16", "127.0.0.2", info["port"])):
        entry = {"attempted": False}
        if time.monotonic() >= deadline:
            observations[name] = {"attempted": False, "result": "budget_exhausted"}
            continue
        try:
            with socket.socket(socket.AF_INET) as connection:
                _timeout(connection, min(deadline, time.monotonic() + .7))
                entry.update({"attempted": True, "tcp_connected": False, "ssh_banner_received": False})
                connection.connect((host, port))
                entry["tcp_connected"] = True
                data = bytearray()
                while len(data) < 512:
                    _timeout(connection, min(deadline, time.monotonic() + .7))
                    chunk = connection.recv(128)
                    if not chunk:
                        entry["result"] = "closed_before_ssh_banner"
                        break
                    data.extend(chunk)
                    if re.search(rb"(?:^|[\r\n])SSH-2\.0-[^\r\n]{1,240}[\r\n]", data):
                        entry["ssh_banner_received"] = True
                        entry["result"] = "ssh_banner"
                        break
                entry.setdefault("result", "non_ssh_or_oversized_banner")
        except (TimeoutError, socket.timeout):
            entry["result"] = "timeout"
        except OSError:
            entry["result"] = "connection_unavailable"
        observations[name] = entry
    return {"available": True, "endpoints": observations}


def _ssh(directory, deadline):
    info = _fixture_info(directory)
    if info is None:
        return {"available": False, "reason": "fixture_identity_unavailable"}
    if any(not (directory / n).is_file() or (directory / n).is_symlink() for n in ("identity", "known_hosts")):
        return {"available": False, "reason": "strict_auth_material_unavailable"}
    argv = ["ssh", "-F", "/dev/null", "-i", str(directory / "identity"), "-p", str(info["port"]),
            "-o", "BatchMode=yes", "-o", "ConnectionAttempts=1", "-o", "ConnectTimeout=2",
            "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-o", "StrictHostKeyChecking=yes",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "UpdateHostKeys=no",
            "-o", "UserKnownHostsFile=" + str(directory / "known_hosts"),
            "root@127.0.0.1", "timeout 3 sh -c " + shlex.quote(_SCRIPT)]
    try:
        remaining = min(4, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError()
        result = subprocess.run(argv, capture_output=True, timeout=remaining)
        if result.returncode == 0:
            fields = _serial_fields(result.stdout)
            if fields.get("complete") is not True or fields.get("script_boot_consistent") is not True:
                return {"available": False, "authentication": "verified", "reason": "guest_boot_unbound",
                        "guest_fields_discarded": True}
            return {"available": True, "authentication": "verified", "fields": fields,
                    "command_complete": True}
        stderr = result.stderr
        if b"Host key verification failed" in stderr or b"REMOTE HOST IDENTIFICATION HAS CHANGED" in stderr:
            kind = "host_key_verification_failed"
        elif b"Permission denied" in stderr:
            kind = "authentication_failed"
        elif b"Connection refused" in stderr:
            kind = "connection_refused"
        elif b"timed out" in stderr:
            kind = "connection_timeout"
        else:
            kind = "ssh_or_command_failed"
        return {"available": False, "reason": kind, "returncode": result.returncode}
    except (TimeoutError, subprocess.TimeoutExpired):
        return {"available": False, "reason": "timeout"}
    except OSError:
        return {"available": False, "reason": "ssh_unavailable"}


def snapshot(directory, budget_seconds=7):
    """Failure-only observations share one absolute deadline; no repair or confirm."""
    started = time.monotonic()
    report = {"schema_version": 2}
    try:
        if type(budget_seconds) not in (int, float) or not math.isfinite(budget_seconds) or not 0 < budget_seconds <= 30:
            raise ValueError()
        directory = Path(directory)
        marker = directory / ".wrtix-vm"
        if not marker.is_file() or marker.is_symlink():
            raise ValueError()
        deadline = started + budget_seconds
        timings = report["observation_intervals_seconds"] = {}
        def observe(name, function, *args):
            beginning = time.monotonic() - started
            report[name] = function(*args)
            timings[name] = {"from": round(beginning, 3), "until": round(time.monotonic() - started, 3)}
        observe("console_before", _console_evidence, directory)
        observe("host_process_before", _host_process, directory)
        observe("qmp", _qmp, directory, min(deadline, time.monotonic() + 2))
        observe("hostfwd", _hostfwd, directory, min(deadline, time.monotonic() + 2.1))
        observe("ssh", _ssh, directory, min(deadline, time.monotonic() + 4))
        observe("serial", _serial, directory, deadline)
        observe("console_after", _console_evidence, directory)
        observe("host_process_after", _host_process, directory)
        first = report["console_before"].get("latest_boot_marker")
        last = report["console_after"].get("latest_boot_marker")
        report["latest_log_marker_unchanged"] = first == last if first and last else None
        ssh_boot = report["ssh"].get("fields", {}).get("boot_id")
        serial_boot = report["serial"].get("fields", {}).get("boot_id")
        report["observed_guest_boot_ids_match"] = ssh_boot == serial_boot if ssh_boot and serial_boot else None
        report["host_process_semantics"] = "host_QEMU_and_TCG_threads_only_not_guest_boot_progress"
    except Exception:
        report["capture_error"] = "invalid_fixture_or_capture_unavailable"
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return report


@contextmanager
def capture_on_failure(directory, output, budget_seconds=7):
    """Keep the original exception even when capture or output writing fails."""
    try:
        yield
    except BaseException:
        try:
            report = snapshot(directory, budget_seconds)
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(report, stream, indent=2)
                stream.write("\n")
        except BaseException:
            pass
        raise
