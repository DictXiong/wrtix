# SPDX-License-Identifier: GPL-2.0-only
"""Compiled resources consumed by the real shell agent, without SSH simulation."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/wrtix"))
sys.path.insert(0, str(ROOT / "tests"))
import compiler
import openwrt
import test_target


class ControllerAgent(unittest.TestCase):
    def setUp(self):
        self.target = test_target.TargetTransaction(methodName="runTest")
        self.target.setUp()
        self.addCleanup(self.target.doCleanups)

    def deploy(self, node, initialize=False):
        fleet = {"schema_version": 1, "kind": "openwrt-fleet", "devices": {"fixture": node}}
        compiled = compiler.compile_fleet(fleet, resolve_secrets=True)["fixture"]
        identity = self.target.run_engine("identity")
        compiled.update({key: identity[key] for key in ("engine_sha256", "guard_sha256")})
        ledger_path = self.target.root / "etc/wrtix/managed.json"
        old = json.loads(ledger_path.read_bytes()) if ledger_path.exists() else {}
        request = {"paths": openwrt.managed_paths(compiled, old),
                   "services": sorted(set(compiled["services"]) | set(old.get("services", [])))}
        baseline = self.target.run_engine("check", data=openwrt.canonical(request))
        transaction, bundle = openwrt.pending_bundle(compiled, baseline, "fixture", 120, old, initialize=initialize)
        self.target.run_engine("stage", data=bundle)
        self.target.run_engine("apply", transaction["token"])
        status = self.target.ready()
        self.assertFalse(status["reboot_required"])
        return compiled, transaction, request

    def test_minimal_registration_preserves_password_network_and_keys(self):
        (self.target.root / "etc/wrtix/identity.json").unlink()
        before = {"etc/shadow": b"root:$6$existing:1:0:99999:7:::\n", "etc/config/network": b"# untouched\n",
                  "etc/dropbear/authorized_keys": b"ssh-ed25519 existing\n",
                  "etc/resolv.conf": b"nameserver 192.0.2.53\n"}
        for path, data in before.items():
            self.target.put(path, data)
        compiled, tx, request = self.deploy({}, initialize=True)
        self.target.run_engine("confirm", tx["token"])
        for path, data in before.items():
            self.assertEqual((self.target.root / path).read_bytes(), data)
        identity = self.target.run_engine("identity")
        self.assertEqual(identity["identity"]["device_id"], "fixture")
        actual = self.target.run_engine("check", data=openwrt.canonical(request))
        self.assertEqual(openwrt.changed_files(compiled, actual, actual["ledger"]), [])

    def test_first_enrollment_rollback_restores_unregistered_baseline(self):
        identity = self.target.root / "etc/wrtix/identity.json"
        identity.unlink()
        self.target.put("etc/example", b"original")
        _, tx, _ = self.deploy({"files": {"/etc/example": {"text": "replacement"}}}, initialize=True)
        self.target.run_engine("rollback", tx["token"])
        self.target.restored()
        self.assertFalse(identity.exists())
        self.assertFalse((self.target.root / "etc/wrtix/managed.json").exists())
        self.assertEqual((self.target.root / "etc/example").read_bytes(), b"original")
        self.assertEqual(self.target.run_engine("identity")["identity"]["device_id"], "")

    def test_controller_rollback_restores_owned_file(self):
        self.target.put("etc/example", b"old")
        compiled, tx, _ = self.deploy({"files": {"/etc/example": {"text": "new", "mode": "0600"}}})
        self.assertEqual((self.target.root / "etc/example").read_bytes(), b"new")
        self.target.run_engine("rollback", tx["token"])
        self.target.restored()
        self.assertEqual((self.target.root / "etc/example").read_bytes(), b"old")

    def test_owned_authorization_can_be_released_without_deletion(self):
        compiled, tx, _ = self.deploy({"users": {"root": {"authorizedKeys": ["ssh-ed25519 managed"]}}})
        self.target.run_engine("confirm", tx["token"])
        compiled, tx, _ = self.deploy({})
        self.target.run_engine("confirm", tx["token"])
        self.assertEqual((self.target.root / "etc/dropbear/authorized_keys").read_bytes(), b"ssh-ed25519 managed\n")
        ledger = json.loads((self.target.root / "etc/wrtix/managed.json").read_bytes())
        self.assertNotIn("etc/dropbear/authorized_keys", ledger["files"])

    def test_required_check_blocks_confirmation_and_rollback_remains_available(self):
        executable = "/usr/libexec/wrtix-managed/reject"
        node = {"files": {executable: {"text": "#!/bin/sh\nexit 1\n", "mode": "0700"}},
                "checks": {"reject": {"phase": "preconfirm", "executable": executable, "input": {"private": "hidden"}}}}
        compiled, tx, _ = self.deploy(node)
        failed = self.target.run_engine("confirm", tx["token"], ok=False)
        self.assertNotIn(b"hidden", failed.stderr)
        self.assertNotEqual(self.target.run_engine("status")["state"], "confirmed")
        self.target.run_engine("rollback", tx["token"])
        self.target.restored()
        self.assertFalse((self.target.root / executable[1:]).exists())


if __name__ == "__main__":
    unittest.main()
