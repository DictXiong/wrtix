#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Bounded capture, boot-window exclusion and original failure preservation."""
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent / "vm"))
import failure_snapshot as capture


class Snapshot(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wrtix-snapshot-")
        self.addCleanup(self.temp.cleanup)
        self.vm = Path(self.temp.name)
        (self.vm / ".wrtix-vm").touch()

    def console(self, tail):
        (self.vm / "console.log").write_text("Linux version synthetic\n" + tail)

    def server(self, name, handler):
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(self.vm / name))
        listener.listen(1)
        listener.settimeout(1)
        release = threading.Event()
        failures = []
        def run():
            try:
                with listener:
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(1)
                        handler(connection, release)
            except (OSError, TimeoutError):
                pass
            except BaseException as error:
                failures.append(error)
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        def cleanup():
            release.set()
            listener.close()
            worker.join(1)
            if failures:
                raise failures[0]
        self.addCleanup(cleanup)

    def test_previous_boot_prompt_never_permits_preinit_input(self):
        self.console("Please press Enter to activate this console.\n"
                     "reboot: Restarting system\nLinux version synthetic\n"
                     "Press the [f] key and hit [enter] to enter failsafe mode\n")
        with patch.object(capture.socket, "socket") as make_socket:
            result = capture._serial(self.vm, time.monotonic() + .1)
        make_socket.assert_not_called()
        self.assertEqual(result["boot_stage"], "boot_or_preinit")
        self.assertFalse(result["available"])

    def test_failsafe_never_receives_input(self):
        self.console("Please press Enter to activate this console.\nEntering failsafe mode\n")
        with patch.object(capture.socket, "socket") as make_socket:
            result = capture._serial(self.vm, time.monotonic() + .1)
        make_socket.assert_not_called()
        self.assertEqual(result["boot_stage"], "failsafe")

    def test_boot_change_after_connect_sends_no_input(self):
        self.console("Please press Enter to activate this console.\n")
        fake = Mock()
        fake.__enter__ = Mock(return_value=fake)
        fake.__exit__ = Mock(return_value=False)
        with patch.object(capture, "_console_gate", side_effect=[("normal_console", 0), ("boot_or_preinit", 100)]):
            with patch.object(capture.socket, "socket", return_value=fake):
                result = capture._serial(self.vm, time.monotonic() + .2)
        fake.sendall.assert_not_called()
        self.assertEqual(result["reason"], "boot_changed")

    def test_hung_qmp_obeys_shared_budget(self):
        self.console("Press the [f] key and hit [enter] to enter failsafe mode\n")
        def handler(connection, release):
            connection.sendall(b'{"QMP":{}}\n')
            connection.recv(4096)
            release.wait(.5)
        self.server("monitor.sock", handler)
        started = time.monotonic()
        result = capture.snapshot(self.vm, budget_seconds=.06)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, .3)
        self.assertGreater(elapsed, .03)
        self.assertEqual(result["qmp"]["reason"], "timeout")
        self.assertFalse(result["serial"]["available"])

    def test_qmp_allows_only_fixed_fields(self):
        self.console("preinit\n")
        def handler(connection, release):
            connection.sendall(b'{"QMP":{"private":"SECRET_SENTINEL"}}\n')
            connection.recv(4096)
            connection.sendall(b'{"return":{},"id":"cap"}\n')
            connection.recv(4096)
            connection.sendall(b'{"return":{"status":"running","running":true,"private":"SECRET_SENTINEL"},"id":"state"}\n')
        self.server("monitor.sock", handler)
        result = capture.snapshot(self.vm, .3)
        self.assertEqual(result["qmp"], {"available": True, "status": "running", "running": True})
        self.assertNotIn("SECRET_SENTINEL", json.dumps(result))

    def test_normal_console_filters_transcript_and_process_arguments(self):
        self.console("Please press Enter to activate this console.\n")
        received = []
        def handler(connection, release):
            self.assertEqual(connection.recv(1), b"\n")
            connection.sendall(b"\r\nroot@OpenWrt:/# ")
            data = bytearray()
            while b"WRTIXSNAP done" not in data or not data.endswith(b" 2>/dev/null\n"):
                data.extend(connection.recv(4096))
            received.append(bytes(data))
            connection.sendall(
                b"\nSECRET_SENTINEL arbitrary transcript\n"
                b"WRTIXSNAP uptime 17\nWRTIXSNAP mac 52:54:00:12:34:56\n"
                b"WRTIXSNAP boot_id 01234567-89ab-cdef-0123-456789abcdef\n"
                b"WRTIXSNAP boot_id SECRET_SENTINEL\n"
                b"WRTIXSNAP address 10.0.2.15/24\nWRTIXSNAP address SECRET_SENTINEL\n"
                b"WRTIXSNAP addresses_scan true\nWRTIXSNAP ssh_scan true\n"
                b"WRTIXSNAP ssh_listen true\nWRTIXSNAP agent_state applied\n"
                b"WRTIXSNAP agent_phase awaiting-resume\n"
                b"WRTIXSNAP process 42 flock S locks_lock_inode_wait\n"
                b"WRTIXSNAP process 43 SECRET_SENTINEL S SECRET_SENTINEL\n"
                b"WRTIXSNAP token SECRET_SENTINEL\nWRTIXSNAP lock_owner 42\n"
                b"WRTIXSNAP done\n")
        self.server("serial.sock", handler)
        result = capture.snapshot(self.vm, .7)
        self.assertTrue(result["serial"]["available"])
        fields = result["serial"]["fields"]
        self.assertEqual(fields["addresses"], ["10.0.2.15/24"])
        self.assertEqual(fields["boot_id"], "01234567-89ab-cdef-0123-456789abcdef")
        self.assertTrue(fields["ssh_listen"])
        self.assertEqual(fields["lock_owners"], [42])
        self.assertEqual(fields["processes"], [{"pid": 42, "comm": "flock", "state": "S", "wchan": "locks_lock_inode_wait"}])
        self.assertNotIn("SECRET_SENTINEL", json.dumps(result))
        self.assertNotIn(b"cmdline", received[0])
        self.assertNotIn(b"/etc/shadow", received[0])

    def test_failed_or_incomplete_scans_do_not_claim_false_or_empty_observations(self):
        result = capture._serial_fields(b"WRTIXSNAP ssh_listen false\nWRTIXSNAP done\n")
        self.assertNotIn("ssh_listen", result)
        for name in ("addresses", "processes", "lock_owners"):
            self.assertNotIn(name, result)
        result = capture._serial_fields(b"WRTIXSNAP ssh_scan true\nWRTIXSNAP ssh_listen false\nWRTIXSNAP addresses_scan true\n")
        self.assertFalse(result["ssh_listen"])
        self.assertEqual(result["addresses"], [])

    def test_broken_channels_preserve_exact_caller_exception(self):
        self.console("preinit\n")
        original = RuntimeError("original failure")
        destination = self.vm / "snapshot.json"
        with self.assertRaises(RuntimeError) as caught:
            with capture.capture_on_failure(self.vm, destination, .1):
                raise original
        self.assertIs(caught.exception, original)
        result = json.loads(destination.read_text())
        self.assertFalse(result["qmp"]["available"])
        self.assertFalse(result["serial"]["available"])
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_capture_and_output_exceptions_never_replace_original(self):
        original = RuntimeError("original failure")
        for replacement in (RuntimeError("SECRET_SENTINEL"), KeyboardInterrupt()):
            with self.subTest(replacement=type(replacement).__name__):
                with patch.object(capture, "snapshot", side_effect=replacement):
                    with self.assertRaises(RuntimeError) as caught:
                        with capture.capture_on_failure(self.vm, self.vm / "unused.json"):
                            raise original
                self.assertIs(caught.exception, original)
        with patch.object(capture, "snapshot", return_value={"safe": True}):
            with patch.object(capture.os, "open", side_effect=OSError("SECRET_SENTINEL")):
                with self.assertRaises(RuntimeError) as caught:
                    with capture.capture_on_failure(self.vm, self.vm / "unused.json"):
                        raise original
        self.assertIs(caught.exception, original)


if __name__ == "__main__":
    unittest.main()
