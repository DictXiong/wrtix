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
import time

_PREFIX = "WRTIXSNAP "
_BOOT = re.compile(rb"SeaBIOS|Linux version [^\r\n]+|reboot: (?:Restarting system|machine restart)")
_PROMPT = re.compile(rb"(?:^|[\r\n])root@OpenWrt:[^\r\n]{0,128}# ?")
_PROCESSES = {"procd", "netifd", "dropbear", "flock", "jq", "kmodloader",
              "rcS", "rc.common", "S03wrtix-agent", "agent", "sh"}
_STATES = {"absent", "staged", "applied", "confirmed", "rolled-back",
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
if addresses=$(ip -o addr show dev eth0 2>/dev/null); then
 printf '%s\n' "$addresses" | awk '$3=="inet"||$3=="inet6"{print "WRTIXSNAP address "$4}'
 printf 'WRTIXSNAP addresses_scan true\n'
fi
if [ -r /proc/net/tcp ] && [ -r /proc/net/tcp6 ]; then
 if listen=$(awk '$4=="0A"&&$2~/:0016$/{yes=1}END{print (yes?"true":"false")}' /proc/net/tcp /proc/net/tcp6); then
  printf 'WRTIXSNAP ssh_scan true\nWRTIXSNAP ssh_listen %s\n' "$listen"
 fi
fi
process_scan=true
for p in /proc/[0-9]*; do
 read -r n < "$p/comm" 2>/dev/null || continue
 case "$n" in procd|netifd|dropbear|flock|jq|kmodloader|rcS|rc.common|S03wrtix-agent|agent|sh)
  pid=$(basename "$p")
  if state=$(awk '/^State:/{print $2}' "$p/status" 2>/dev/null) && wait=$(cat "$p/wchan" 2>/dev/null); then
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


def _console_gate(directory):
    path = directory / "console.log"
    if not path.is_file() or path.is_symlink():
        return "unknown", None
    with path.open("rb") as stream:
        stream.seek(0, 2)
        offset = max(0, stream.tell() - 131072)
        stream.seek(offset)
        data = stream.read(131072)
    markers = list(_BOOT.finditer(data))
    if not markers:
        return "unknown", None
    marker = markers[-1]
    tail = data[marker.start():]
    identity = offset + marker.start()
    if re.search(rb"^(?:Entering failsafe|FAILSAFE MODE|failsafe mode active)", tail, re.I | re.M):
        return "failsafe", identity
    if b"Please press Enter to activate this console." in tail:
        return "normal_console", identity
    return "boot_or_preinit", identity


def _serial_fields(transcript):
    fields = {}
    lists = {"addresses": [], "processes": [], "lock_owners": []}
    for raw in transcript.decode("utf-8", "replace").replace("\r", "").splitlines():
        if not raw.startswith(_PREFIX):
            continue
        parts = raw[len(_PREFIX):].split()
        if not parts:
            continue
        key, values = parts[0], parts[1:]
        if key == "uptime" and len(values) == 1 and re.fullmatch(r"\d{1,10}", values[0]):
            fields["uptime_seconds"] = int(values[0])
        elif key == "boot_id" and len(values) == 1 and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", values[0]):
            fields["boot_id"] = values[0]
        elif key == "mac" and len(values) == 1 and re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", values[0]):
            fields["mac"] = values[0].lower()
        elif key == "address" and len(values) == 1 and len(values[0]) <= 64:
            try:
                address = str(ipaddress.ip_interface(values[0]))
                if address not in lists["addresses"] and len(lists["addresses"]) < 16:
                    lists["addresses"].append(address)
            except ValueError:
                pass
        elif key in ("ssh_listen", "addresses_scan", "ssh_scan", "process_scan", "lock_scan", "reload_complete") and values in (["true"], ["false"]):
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
    for name, marker in (("addresses", "addresses_scan"), ("processes", "process_scan"), ("lock_owners", "lock_scan")):
        if lists[name] or fields.get(marker) is True:
            fields[name] = lists[name]
    if fields.get("ssh_scan") is not True:
        fields.pop("ssh_listen", None)
    return fields


def _receive(connection, deadline, predicate, data=None):
    data = bytearray() if data is None else data
    while not predicate(data):
        _timeout(connection, deadline)
        chunk = connection.recv(4096)
        if not chunk:
            raise EOFError()
        data.extend(chunk)
        if len(data) > 65536:
            raise ValueError()
    return bytes(data)


def _serial(directory, deadline):
    stage, boot_identity = _console_gate(directory)
    if stage != "normal_console":
        return {"available": False, "reason": "normal_console_unavailable", "boot_stage": stage}
    transcript = bytearray()
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            _timeout(connection, deadline)
            connection.connect(str(directory / "serial.sock"))
            if _console_gate(directory) != (stage, boot_identity):
                return {"available": False, "reason": "boot_changed"}
            _timeout(connection, deadline)
            connection.sendall(b"\n")
            _receive(connection, deadline, lambda data: bool(_PROMPT.search(data)))
            if _console_gate(directory) != (stage, boot_identity):
                return {"available": False, "reason": "boot_changed"}
            command = ("timeout 3 sh -c " + shlex.quote(_SCRIPT) + " 2>/dev/null\n").encode()
            if len(command) > 4000:
                raise ValueError()
            _timeout(connection, deadline)
            connection.sendall(command)
            _receive(connection, deadline, lambda data: b"\nWRTIXSNAP done" in data.replace(b"\r", b""), transcript)
            return {"available": True, "boot_stage": stage, "fields": _serial_fields(transcript)}
    except (TimeoutError, socket.timeout):
        return {"available": False, "reason": "timeout", "boot_stage": stage,
                "partial_fields": _serial_fields(transcript)}
    except (OSError, EOFError, ValueError, TypeError):
        return {"available": False, "reason": "channel_unavailable_or_invalid", "boot_stage": stage,
                "partial_fields": _serial_fields(transcript)}


def snapshot(directory, budget_seconds=7):
    """Return only safe fields; all socket operations share one total deadline."""
    started = time.monotonic()
    report = {"schema_version": 1}
    try:
        if type(budget_seconds) not in (int, float) or not math.isfinite(budget_seconds) or not 0 < budget_seconds <= 10:
            raise ValueError()
        directory = Path(directory)
        marker = directory / ".wrtix-vm"
        if not marker.is_file() or marker.is_symlink():
            raise ValueError()
        deadline = started + budget_seconds
        report["qmp"] = _qmp(directory, min(deadline, time.monotonic() + 2))
        report["serial"] = _serial(directory, deadline)
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
