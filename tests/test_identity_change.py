# SPDX-License-Identifier: GPL-2.0-only
"""Controller and real shell transactions for ordinary device ID changes.

Only the SSH transport and physical reboot are simulated in this fixture.
Identity, ownership, staging, checks, deadlines and recovery run the real agent.
"""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/wrtix"))
sys.path.insert(0, str(ROOT / "tests"))
import compiler
import openwrt
import test_target


class IdentityChangeTests(unittest.TestCase):
    def setUp(self):
        self.target = test_target.TargetTransaction(methodName="runTest")
        self.target.setUp()
        self.addCleanup(self.target.doCleanups)
        self.calls, self.hook = [], None
        owner = self

        class LocalTransport(openwrt.Transport):
            def run(self, command, data=None, timeout=60):
                if command.startswith("if test -f /" + openwrt.LEDGER):
                    path = owner.target.root / openwrt.LEDGER
                    return path.read_bytes() if path.exists() else b"{}"
                words = shlex.split(command)
                if words[:2] != ["sh", openwrt.REMOTE]:
                    raise AssertionError("unexpected transport command")
                action, *args = words[2:]
                owner.calls.append(action)
                if owner.hook:
                    owner.hook(action)
                result = subprocess.run(["sh", str(test_target.ENGINE), "--test-root", str(owner.target.root),
                                         action, *args], input=data, capture_output=True, timeout=timeout,
                                        env=owner.target.engine_env)
                if result.returncode:
                    raise openwrt.DeployError(openwrt.remote_failure(result.stderr, result.returncode))
                if action == "reboot":
                    owner.target.new_boot()
                return result.stdout

        self.transport = LocalTransport
        self.node = {"target_host": "router.example", "files": {"/etc/example": {"text": "old\n"}}}
        self.receipt = self.target.root / "receipt.json"

    def controller(self, action, name="next", node=None, *, options=(), error=None, recovery=False):
        self.calls.clear()
        fleet = {"schema_version": 1, "kind": "openwrt-fleet", "devices": {name: node or self.node}}
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(openwrt, "Transport", self.transport), \
                patch.object(openwrt, "load_manifest", return_value=fleet,
                             side_effect=AssertionError("recovery must not load manifest") if recovery else None), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            args = [action, "--device", name, "--quiet", *options]
            if error:
                with self.assertRaisesRegex(openwrt.DeployError, error):
                    openwrt.main(args)
            else:
                openwrt.main(args)
        return (json.loads(stdout.getvalue()) if stdout.getvalue() else None), stderr.getvalue()

    def seed(self):
        result, _ = self.controller("deploy", "fixture", options=("--auto-confirm",))
        self.assertEqual(result["state"], "confirmed")
        return {path: (self.target.root / path).read_bytes()
                for path in ("etc/example", "etc/wrtix/identity.json", openwrt.LEDGER)}

    def bundle(self, name="next", node=None):
        fleet = {"schema_version": 1, "kind": "openwrt-fleet", "devices": {name: node or self.node}}
        compiled = compiler.compile_fleet(fleet, resolve_secrets=True)[name]
        identity = self.target.run_engine("identity")
        compiled.update({key: identity[key] for key in ("engine_sha256", "guard_sha256")})
        old = json.loads((self.target.root / openwrt.LEDGER).read_bytes())
        request = {"paths": openwrt.managed_paths(compiled, old), "services": list(compiled["services"])}
        baseline = self.target.run_engine("check", data=openwrt.canonical(request))
        return openwrt.pending_bundle(compiled, baseline, name, 120, old)

    def assert_restored(self, before):
        self.target.restored()
        for path, data in before.items():
            self.assertEqual((self.target.root / path).read_bytes(), data, path)
        self.assertEqual(self.target.run_engine("identity")["identity"]["device_id"], "fixture")

    def test_normal_deploy_warns_confirms_and_repeats_as_noop(self):
        self.seed()
        # No --init or new migration option; the only file change is identity.
        result, warning = self.controller("deploy", options=("--auto-confirm", "--receipt", str(self.receipt)))
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual(result["changed_paths"], ["etc/wrtix/identity.json"])
        self.assertIn("fixture -> next [target root@router.example:22]", warning)
        receipt = json.loads(self.receipt.read_bytes())
        self.assertEqual(receipt["device_id"], "next")
        self.assertEqual(receipt["expected_identity"]["device_id"], "next")
        self.assertEqual(receipt["initial_identity"]["device_id"], "fixture")
        self.assertFalse(receipt.get("initialize", False))
        snapshot = self.target.check(["etc/wrtix/identity.json"])
        self.assertEqual(snapshot["identity"]["device_id"], "next")
        self.assertEqual(snapshot["ledger"]["device_id"], "next")
        noop, warning = self.controller("deploy", options=("--auto-confirm",))
        self.assertEqual(noop["state"], "noop")
        self.assertEqual(warning, "")
        self.assertNotIn("stage", self.calls)

    def test_package_preparation_warns_before_action_without_changing_identity(self):
        before = self.seed()
        # No package installation is needed, but preparation still runs the real
        # agent's separate lock/fence path using a synthetic package manager.
        self.target.put("package-tools/opkg", b"#!/bin/sh\nexit 0\n", 0o700)
        self.target.engine_env["PATH"] = str(self.target.root / "package-tools") + os.pathsep + self.target.engine_env["PATH"]
        observed = []
        def action(name):
            if name == "package-prepare":
                observed.append(sys.stderr.getvalue())
        self.hook = action
        result, warning = self.controller("prepare-packages")
        self.assertEqual(result["state"], "packages-ready")
        self.assertEqual(observed, [warning])
        self.assertIn("fixture -> next [target root@router.example:22]", warning)
        self.assertFalse(set(self.calls) & {"stage", "apply", "confirm", "rollback"})
        for path, data in before.items():
            self.assertEqual((self.target.root / path).read_bytes(), data)

    def test_remote_preview_diff_and_verify_do_not_write(self):
        before = self.seed()
        state = self.target.root / "etc/wrtix/transactions/state"
        manifest, lease = (state / "manifest.json").read_bytes(), (state / "lease").read_bytes()
        for action, options, error in (("plan", ("--remote",), None),
                                       ("plan", ("--remote", "--diff"), None),
                                       ("verify", ("--diff",), "configuration differs")):
            with self.subTest(action=action, options=options):
                result, warning = self.controller(action, options=options, error=error)
                self.assertIn("fixture -> next", warning)
                if action == "plan":
                    self.assertEqual(result["identity_change"], {"from": "fixture", "to": "next"})
                else:
                    self.assertFalse(result["matches"])
                self.assertFalse(set(self.calls) & {"stage", "apply", "confirm", "rollback"})
        self.assertEqual((state / "manifest.json").read_bytes(), manifest)
        self.assertEqual((state / "lease").read_bytes(), lease)
        for path, data in before.items():
            self.assertEqual((self.target.root / path).read_bytes(), data)

    def test_explicit_and_expiry_rollback_restore_old_identity_and_ownership(self):
        before = self.seed()
        changed = {**self.node, "files": {"/etc/example": {"text": "new\n"},
                                         "/etc/new-owned": {"text": "candidate\n"}}}
        for expire in (False, True):
            with self.subTest(expire=expire):
                self.receipt.unlink(missing_ok=True)
                result, _ = self.controller("deploy", node=changed, options=("--receipt", str(self.receipt)))
                self.assertEqual(result["state"], "applied")
                state = self.target.root / "etc/wrtix/transactions/state"
                lease = (state / "lease").read_bytes()
                if expire:
                    deadline = self.target.run_engine("status")["deadline"]
                    self.target.put("proc/uptime", f"{deadline + 1}.00 0.00\n".encode())
                    self.target.run_engine("early")
                else:
                    self.controller("rollback", options=("--receipt", str(self.receipt)), recovery=True)
                self.assert_restored(before)
                self.assertFalse((self.target.root / "etc/new-owned").exists())
                result, _ = self.controller("status", options=("--receipt", str(self.receipt)), recovery=True)
                self.assertEqual(result["state"], "rolled-back")
                self.assertEqual(result["device_id"], "next")  # Transaction ID remains its candidate ID.
                saved = json.loads(self.receipt.read_bytes())
                self.assertEqual(saved["initial_identity"]["device_id"], "fixture")
                self.assertEqual((state / "lease").read_bytes(), lease)

    def test_receipt_confirm_needs_no_flake_and_keeps_the_lease(self):
        self.seed()
        self.controller("deploy", options=("--receipt", str(self.receipt)))
        state = self.target.root / "etc/wrtix/transactions/state"
        lease = (state / "lease").read_bytes()
        result, _ = self.controller("confirm", options=("--receipt", str(self.receipt)), recovery=True)
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual((state / "lease").read_bytes(), lease)

    def test_invalid_or_unenrolled_identity_and_old_contract_reject_before_stage(self):
        self.seed()
        path = self.target.root / "etc/wrtix/identity.json"
        for invalid in (None, {}, {"schema_version": 1, "device_id": ""},
                        {"schema_version": 1, "device_id": "bad\nname"}):
            with self.subTest(invalid=invalid):
                path.unlink(missing_ok=True)
                if invalid is not None:
                    self.target.put("etc/wrtix/identity.json", json.dumps(invalid).encode())
                self.controller("deploy", error="valid registered identity|invalid_identity")
                self.assertNotIn("stage", self.calls)
        self.target.put("etc/wrtix/identity.json", b'{"schema_version":1,"device_id":"fixture"}')
        contract = json.loads((ROOT / "agent/contract.json").read_bytes())
        del contract["transactional_identity_change"]
        self.target.put("usr/share/wrtix/contract.json", json.dumps(contract).encode())
        self.controller("deploy", error="install the matching wrtix-agent")
        self.assertNotIn("stage", self.calls)

    def test_board_and_ledger_owner_remain_strict(self):
        self.seed()
        node = {**self.node, "identity": {"board_names": ["example,other"]}}
        self.controller("deploy", node=node, error="target board differs")
        self.assertNotIn("stage", self.calls)
        path = self.target.root / openwrt.LEDGER
        ledger = json.loads(path.read_bytes())
        ledger["device_id"] = "unrelated"
        self.target.put(openwrt.LEDGER, json.dumps(ledger).encode())
        self.controller("deploy", error="ledger belongs to another device")
        self.assertNotIn("stage", self.calls)

    def test_identity_and_ledger_races_at_stage_and_apply_are_rejected(self):
        before = self.seed()
        for phase in ("stage", "apply"):
            for path in ("etc/wrtix/identity.json", openwrt.LEDGER):
                with self.subTest(phase=phase, path=path):
                    tx, bundle = self.bundle()
                    if phase == "apply":
                        self.target.run_engine("stage", data=bundle)
                    document = json.loads((self.target.root / path).read_bytes())
                    document["device_id"] = "concurrent"
                    self.target.put(path, json.dumps(document).encode())
                    failed = self.target.run_engine(phase, *([tx["token"]] if phase == "apply" else []),
                                                    data=bundle if phase == "stage" else None, ok=False)
                    self.assertIn(b"baseline_changed", failed.stderr)
                    self.assertEqual((self.target.root / "etc/example").read_bytes(), before["etc/example"])
                    self.target.put(path, before[path])
                    if phase == "apply":
                        self.target.run_engine("rollback", tx["token"])

    def test_controller_rejects_a_changed_identity_before_baseline(self):
        self.seed()
        def change(action):
            if action == "check":
                self.target.put("etc/wrtix/identity.json", b'{"schema_version":1,"device_id":"concurrent"}')
        self.hook = change
        self.controller("deploy", error="identity or boot changed before staging")
        self.assertNotIn("stage", self.calls)

    def test_check_running_is_not_disabled_for_a_rename(self):
        self.seed()
        self.target.service(enabled=True, running=True)
        node = {**self.node, "services": {"fixture": {"enable": True, "running": True}}}
        self.controller("deploy", node=node, options=("--receipt", str(self.receipt)))
        self.target.put("run/openwrt-test-services/fixture.running", b"false\n")
        self.controller("confirm", options=("--receipt", str(self.receipt)), recovery=True,
                        error="confirmation_readback_mismatch")
        self.controller("rollback", options=("--receipt", str(self.receipt)), recovery=True)
        self.target.restored()

    def test_identity_change_with_mac_reboot_confirms_or_restores_old_identity(self):
        self.target.service("network", enabled=True, running=True)
        self.target.put("etc/config/network", self.target.mac_network("02:00:00:00:00:01"))
        self.node = {"target_host": "router.example", "uci": {"network": {
            "mac_cpu": {"type": "device", "options": {"name": "eth0", "macaddr": "02:00:00:00:00:01"}}}},
            "files": {"/etc/example": {"text": "old\n"}},
            "services": {"network": {"enable": True, "running": True}}}
        before = self.seed()
        changed = copy.deepcopy(self.node)
        changed["uci"]["network"]["mac_cpu"]["options"]["macaddr"] = "02:00:00:00:00:02"
        self.controller("deploy", node=changed, options=("--receipt", str(self.receipt)))
        status = self.target.run_engine("status")
        self.assertTrue(status["reboot_required"])
        self.assertEqual(status["reboot_state"], "resumed")
        self.assertNotEqual(status["current_boot_id"], status["origin_boot_id"])
        self.controller("rollback", options=("--receipt", str(self.receipt)), recovery=True)
        self.assert_restored(before)
        self.receipt.unlink()
        result, _ = self.controller("deploy", node=changed, options=("--auto-confirm", "--receipt", str(self.receipt)))
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual(self.target.run_engine("identity")["identity"]["device_id"], "next")


if __name__ == "__main__":
    unittest.main()
