#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Synthetic checks for the existing MAC transaction path and bounded failures."""
import argparse
import copy
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/vm"))
from controller_acceptance import Acceptance
import openwrt

NETWORK = b"config interface 'lan'\n option device 'eth0'\n option proto 'static'\n option ipaddr '10.0.2.15'\n"
ORIGINAL_MAC = b"52:54:00:12:34:56"


class Probe(Acceptance):
    def __init__(self, args):
        super().__init__(args)
        self.calls, self.boot, self.mac, self.enrolled = [], "boot0", ORIGINAL_MAC, False
        self.initialize_state, self.restore_error, self.restore_fault = "confirmed", None, None

    def passed(self, name):
        self.results.append(name)

    def command(self, action, node=None, *, name, options=(), **kwargs):
        self.calls.append((action, copy.deepcopy(node), name, tuple(options), kwargs))
        if name == "initialize":
            self.enrolled = True
            return {"state": self.initialize_state}
        if name == "planned-reboot":
            self.boot, self.mac = "boot1", b"02:00:00:00:00:27"
        if name == "restore-mac":
            if self.restore_error:
                raise self.restore_error
            self.boot = "boot1" if self.restore_fault == "boot" else "boot2"
            self.mac = b"02:00:00:00:00:27" if self.restore_fault == "mac" else ORIGINAL_MAC
        return {"state": "confirmed"}

    def remote(self, command, **kwargs):
        if command == "/usr/libexec/wrtix/agent identity":
            output = json.dumps({"identity": {"device_id": "vm" if self.enrolled else ""}, "boot_id": self.boot}).encode()
        elif command.startswith(". /etc/openwrt_release"):
            output = b"25.12.0"
        elif command.startswith("sha256sum "):
            output = b"unchanged synthetic network/authentication digest\n"
        elif command == "cat /etc/config/network":
            output = NETWORK
        elif command == "cat /sys/class/net/eth0/address":
            output = self.mac + b"\n"
        elif command == "uci -q get network.lan.ipaddr":
            output = b"10.0.2.16\n"
        else:
            raise AssertionError("unexpected probe: " + command)
        return SimpleNamespace(stdout=output, returncode=0)


class FocusedMAC(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wrtix-mac-focus-")
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name)
        self.args = argparse.Namespace(output=path / "output", key=path / "identity",
                                       known_hosts=path / "known_hosts", host="127.0.0.1",
                                       alternate_host="127.0.0.2", port=22080)

    def test_same_address_prefix_and_two_guarded_mac_boots_with_original_budgets(self):
        probe = Probe(self.args)
        result = probe.run_mac_diagnostic()
        self.assertEqual([c[2] for c in probe.calls],
                         ["initialize", "handoff", "handoff-confirm", "restore-address", "planned-reboot", "restore-mac"])
        self.assertIn("StrictHostKeyChecking=yes", probe.ssh)
        for _, node, name, options, _ in probe.calls:
            if name in ("planned-reboot", "restore-mac"):
                self.assertEqual(options[options.index("--ttl") + 1], "300")
                self.assertIn("--auto-confirm", options)
                self.assertNotIn("--wait", options)  # unchanged controller default
                self.assertEqual(node["target_host"], "127.0.0.1")
        handoff = probe.calls[1]
        self.assertEqual(handoff[4]["address_input"], "127.0.0.2")
        self.assertIn("--ask-post-switch-host", handoff[3])
        self.assertNotIn("macaddr", result["uci"]["network"]["lan"]["options"])
        self.assertEqual(json.loads((self.args.output / "summary.json").read_text())["outcome"], "not-reproduced")
        original_parse = argparse.ArgumentParser.parse_args
        class Captured(Exception): pass
        def capture(parser, argv=None, *args, **kwargs):
            parsed = original_parse(parser, argv, *args, **kwargs)
            self.assertEqual(parsed.wait, 240)
            raise Captured()
        with patch.object(argparse.ArgumentParser, "parse_args", capture), self.assertRaises(Captured):
            openwrt.main(["deploy", "--device", "vm", "--ttl", "300"])

    def test_unconfirmed_initialization_stops_before_network_changes(self):
        probe = Probe(self.args); probe.initialize_state = "applied"
        with self.assertRaises(AssertionError): probe.run_mac_diagnostic()
        self.assertEqual([c[2] for c in probe.calls], ["initialize"])

    def test_second_boot_and_restored_physical_mac_remain_required(self):
        for fault in ("boot", "mac"):
            with self.subTest(fault=fault):
                probe = Probe(self.args); probe.restore_fault = fault
                with self.assertRaises(AssertionError): probe.run_mac_diagnostic()
                self.assertNotIn("original network and MAC restored through second guarded reboot", probe.results)

    def test_original_mac_transaction_error_is_not_swallowed(self):
        probe = Probe(self.args); failure = RuntimeError("synthetic restore failure"); probe.restore_error = failure
        with self.assertRaises(RuntimeError) as raised: probe.run_mac_diagnostic()
        self.assertIs(raised.exception, failure)

    def test_failed_child_status_probe_is_one_second_and_keeps_original_failure(self):
        probe = Acceptance(self.args)
        probe.remote = Mock(side_effect=subprocess.TimeoutExpired("private synthetic SSH", 1))
        result = subprocess.CompletedProcess([], 1, b"", b"openwrt-deploy: target reboot not verified; automatic rollback remains armed\n")
        with patch("controller_acceptance.subprocess.run", return_value=result), contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "failed-child returned 1"):
            probe.command("deploy", name="failed-child")
        probe.remote.assert_called_once_with("/usr/libexec/wrtix/agent status", check=False, timeout=1)
        diagnostic = json.loads((self.args.output / "failure.json").read_text())
        self.assertEqual(diagnostic["returncode"], 1)
        self.assertFalse(diagnostic["status_available"])


if __name__ == "__main__":
    unittest.main()
