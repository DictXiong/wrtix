"""Display contract and real agent transaction comparisons using synthetic data."""
import base64
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/wrtix"))
sys.path.insert(0, str(ROOT / "tests"))
import compiler
import config_diff
import openwrt
import rebuild
import test_controller_agent


def metadata(files, modes=None, **extra):
    return {"files": {p: compiler.digest(v) if v is not None else None for p, v in files.items()},
            "modes": {p: (modes or {}).get(p, "600" if v is not None else None) for p, v in files.items()}, **extra}


class DisplayTests(unittest.TestCase):
    def test_content_creation_removal_modes_binary_and_no_final_newline(self):
        before = {"etc/change": b"old\n", "etc/remove": b"remove", "etc/mode": b"same", "etc/binary": b"\0old"}
        after = {"etc/change": b"new", "etc/add": b"add\n", "etc/mode": b"same", "etc/binary": b"\0new"}
        target = metadata({**{p: None for p in before}, **after}, {"etc/mode": "644"})
        output = config_diff.render_diff("fixture", metadata(before), target, before, after)
        for text in ("-old", "+new", "No newline at end of file", "etc/add", "etc/remove", "600 -> 644", "binary content differs"):
            self.assertIn(text, output)

    def test_secret_metadata_services_resolver_and_terminal_escapes(self):
        current = metadata({"etc/token": b"secret-before"}, services={"demo": {"enabled": False, "running": False}}, resolver_state={"type": "file", "mode": "644"})
        target = metadata({"etc/token": b"secret-after"}, services={"demo": {"enabled": True, "running": None}}, resolver_state={"type": "symlink", "target": "/tmp/resolv"})
        output = config_diff.render_diff("fixture", current, target, {}, {}, opaque=["etc/token"], root_sync=True)
        self.assertNotIn("secret-before", output)
        self.assertNotIn("secret-after", output)
        self.assertIn("content hidden", output)
        self.assertIn("service demo enabled", output)
        self.assertNotIn("service demo running", output)
        self.assertIn("/etc/resolv.conf", output)
        self.assertIn("root password", output)
        self.assertEqual(config_diff.safe_text("\x1b[31m\rhello"), "\\x1b[31m\\x0dhello")

    def test_content_is_bound_to_snapshot_and_missing_content_fails_closed(self):
        current = metadata({"etc/example": b"old"})
        with self.assertRaises(config_diff.DiffError):
            config_diff.decode_contents(current, {"etc/example": base64.b64encode(b"changed").decode()})
        with self.assertRaises(config_diff.DiffError):
            config_diff.render_diff("fixture", current, metadata({"etc/example": b"new"}), {}, {})

    def test_eof_newline_change_is_a_real_content_diff(self):
        before, after = {"etc/example": b"same"}, {"etc/example": b"same\n"}
        output = config_diff.render_diff("fixture", metadata(before), metadata(after), before, after)
        self.assertIn("-same\n\\ No newline at end of file\n+same\n", output)

    def test_generic_provenance_in_structured_resources_and_root_resource(self):
        secret = {"sops": {"file": "/synthetic/encrypted.yaml", "key": "arbitrary-field"}}
        node = {"resourceSets": {"extension": {"uci": {"demo": {"named": {"type": "example", "options": {"ordinary_name": secret}}}},
                 "files": {"/etc/json": {"format": "json", "value": {"nested": [secret]}}}}},
                "files": {"/etc/public": {"text": "normal", "mode": "0600"}},
                "users": {"root": {"passwordHash": secret}}}
        fleet = {"schema_version": 1, "kind": "openwrt-fleet", "devices": {"fixture": node}}
        compiled = compiler.compile_fleet(fleet)["fixture"]
        self.assertEqual(compiled["sensitive_paths"], ["etc/config/demo", "etc/json", "etc/wrtix/root-password-hash"])
        self.assertNotIn("etc/public", compiled["sensitive_paths"])

    def test_wrapper_accepts_supported_verbs_and_rejects_offline_diff(self):
        for verb in ("verify", "test", "switch", "dry-run", "confirm", "rollback"):
            extra = ["--remote"] if verb == "dry-run" else []
            extra += ["--receipt", "/tmp/fixture"] if verb in ("confirm", "rollback") else ["--device", "fixture"]
            args, _ = rebuild.parse_args([verb, "--diff", *extra])
            self.assertIn("--diff", rebuild.controller_args(args, {"switch": "deploy", "test": "deploy", "dry-run": "plan"}.get(verb, verb)))
        args, recovery = rebuild.parse_args(["switch", "--receipt", "/tmp/fixture", "--diff"])
        self.assertTrue(recovery)
        self.assertIn("--diff", rebuild.controller_args(args, "confirm"))
        for verb in ("build", "dry-run", "status", "prepare-packages", "receipts", "clean", "import"):
            with self.subTest(verb=verb), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                rebuild.parse_args([verb, "--diff", "--device", "fixture"])

    def test_provenance_does_not_change_valid_equal_producer_merges(self):
        secret = {"secretRef": {"provider": "synthetic", "key": "token"}}
        def section(value):
            return {"type": "demo", "options": {"arbitrary": value, "other": "initial"}}
        node = {"resourceSets": {"owner": {"uci": {"demo": {"s": section(secret)}}}},
                "uci": {"demo": {"s": section("same-value")}},
                "overrides": {"uci": {"demo": {"s": {"options": {"other": secret}}}}}}
        fleet = {"schema_version": 1, "kind": "openwrt-fleet", "devices": {"fixture": node}}
        resolved = json.loads(json.dumps(fleet["devices"]).replace(json.dumps(secret), '"same-value"'))
        compiled = compiler.compile_fleet(fleet, resolve_secrets=True, resolver=Mock(resolve=Mock(return_value=resolved)))["fixture"]
        self.assertEqual(compiled["sensitive_paths"], ["etc/config/demo"])
        self.assertIn(b"same-value", compiled["files"]["etc/config/demo"])


class DiffAgentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_controller_agent.ControllerAgent(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.target = self.fixture.target

    def request(self, paths, opaque=()):
        return self.target.run_engine("diff", data=openwrt.canonical({"paths": paths, "services": [], "opaque_paths": list(opaque)}))

    def controller(self, argv, node=None, *, expect_error=False, no_manifest=False):
        target = self.target
        calls = []
        class LocalTransport:
            host, port, user, host_key_alias = "router.example", 22, "root", None
            def __init__(self, args, manifest, compiled, name, **kwargs):
                self.compiled = compiled
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def close(self):
                pass
            def reconnect(self, *args, **kwargs):
                return self
            def agent_preflight(self, **kwargs):
                identity = target.run_engine("identity")
                self.compiled.update({key: identity[key] for key in ("engine_sha256", "guard_sha256")})
                return identity
            def installed(self):
                return True
            def ensure_engine(self):
                self.agent_preflight()
            def ledger(self):
                path = target.root / "etc/wrtix/managed.json"
                return json.loads(path.read_bytes()) if path.exists() else {}
            def call(self, command, token=None, data=None):
                calls.append(command)
                return target.run_engine(command, *([token] if token else []), data=data)
        fleet = {"schema_version": 1, "kind": "openwrt-fleet", "devices": {"fixture": node or {}}}
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(openwrt, "Transport", LocalTransport), \
                patch.object(openwrt, "load_manifest", side_effect=AssertionError("receipt must not evaluate") if no_manifest else None, return_value=fleet), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            if expect_error:
                with self.assertRaises(openwrt.DeployError):
                    openwrt.main([*argv, "--quiet"])
            else:
                openwrt.main([*argv, "--quiet"])
        return json.loads(stdout.getvalue()) if stdout.getvalue() else None, stderr.getvalue(), calls

    def test_verify_json_exit_behavior_and_live_plan_are_compatible(self):
        self.target.put("etc/example", b"old\n")
        node = {"files": {"/etc/example": {"text": "new\n"}}}
        plain, stderr, _ = self.controller(["verify", "--device", "fixture"], node, expect_error=True)
        self.assertEqual(stderr, "")
        compared, stderr, calls = self.controller(["verify", "--device", "fixture", "--diff"], node, expect_error=True)
        self.assertEqual(plain, compared)
        self.assertFalse(compared["matches"])
        self.assertIn("-old", stderr)
        self.assertIn("+new", stderr)
        self.assertNotIn("stage", calls)
        planned, stderr, calls = self.controller(["plan", "--remote", "--device", "fixture", "--diff"], node)
        self.assertTrue(planned["secret_values_compared"])
        self.assertTrue(planned["remote_identity_verified"])
        self.assertIn("+new", stderr)
        self.assertNotIn("verify", calls)
        self.assertNotIn("stage", calls)

    def test_test_receipt_rollback_and_switch_noop_keep_json_separate(self):
        self.target.put("etc/example", b"original\n")
        receipt = str(self.target.root / "test-receipt.json")
        node = {"files": {"/etc/example": {"text": "candidate\n"}}}
        result, stderr, calls = self.controller(["deploy", "--device", "fixture", "--receipt", receipt, "--diff"], node)
        self.assertEqual(result["state"], "applied")
        self.assertIn("-original", stderr)
        self.assertLess(calls.index("diff"), calls.index("stage"))
        self.assertNotIn("confirm", calls)
        result, stderr, _ = self.controller(["rollback", "--receipt", receipt, "--diff"], no_manifest=True)
        self.assertIn("+original", stderr)
        self.target.restored()
        Path(receipt).unlink()
        result, _, calls = self.controller(["deploy", "--device", "fixture", "--receipt", receipt, "--auto-confirm", "--diff"], node)
        self.assertEqual(result["state"], "confirmed")
        self.assertIn("confirm", calls)
        noop, stderr, calls = self.controller(["deploy", "--device", "fixture", "--auto-confirm", "--diff"], node)
        self.assertEqual(noop["state"], "noop")
        self.assertIn("no differences", stderr)
        self.assertNotIn("stage", calls)

    def test_confirmation_from_receipt_needs_no_flake_and_keeps_deadline(self):
        receipt = str(self.target.root / "test-receipt.json")
        node = {"files": {"/etc/example": {"text": "candidate\n"}}}
        self.controller(["deploy", "--device", "fixture", "--receipt", receipt], node)
        state = self.target.root / "etc/wrtix/transactions/state"
        lease = (state / "lease").read_bytes()
        result, stderr, calls = self.controller(["confirm", "--receipt", receipt, "--diff"], no_manifest=True)
        self.assertEqual(result["state"], "confirmed")
        self.assertIn("no differences", stderr)
        self.assertLess(calls.index("diff"), calls.index("confirm"))
        self.assertEqual((state / "lease").read_bytes(), lease)

    def test_read_only_snapshot_has_no_plaintext_or_secret_derived_bytes(self):
        # OpenWrt jq can be built without the Oniguruma regex library.
        self.target.fault_tool("jq", needle=("gsub(", "match(", "test("))
        self.target.put("etc/example", b"old\n")
        self.target.put("etc/token", b"private-value")
        response = self.request(["etc/example", "etc/token"], ["etc/token"])
        self.assertEqual(config_diff.decode_contents(response["current"], response["contents"]), {"etc/example": b"old\n"})
        self.assertNotIn("private-value", json.dumps(response))
        self.assertFalse((self.target.root / "etc/wrtix/transactions/state").exists())
        self.assertEqual(list((self.target.root / "tmp").glob("wrtix-diff.*")), [])
        # A rejected request must not allow arbitrary reads or leave private files.
        self.target.run_engine("diff", data=b'{"paths":["etc/shadow"],"services":[],"opaque_paths":[]}', ok=False)
        self.assertEqual(list((self.target.root / "tmp").glob("wrtix-diff.*")), [])

    def test_transaction_targets_restore_original_content_not_new_configuration(self):
        self.target.put("etc/example", b"original\n", 0o644)
        _, tx, _ = self.fixture.deploy({"files": {"/etc/example": {"text": "candidate\n"}}})
        state = self.target.root / "etc/wrtix/transactions/state"
        lease = (state / "lease").read_bytes()
        self.target.put("etc/example", b"manual drift\n")
        for action, expected in (("confirm", b"candidate\n"), ("rollback", b"original\n")):
            stderr = io.StringIO()
            transport = Mock()
            transport.call.side_effect = lambda command, token=None, data=None: self.target.run_engine(command, *([token] if token else []), data=data)
            with contextlib.redirect_stderr(stderr):
                openwrt.transaction_diff(transport, {"token": tx["token"], "device_id": "fixture"}, action)
            self.assertIn(expected.decode().strip(), stderr.getvalue())
            self.assertIn("-manual drift", stderr.getvalue())
            self.assertEqual((state / "lease").read_bytes(), lease)
        self.assertFalse((state / "confirmed").exists())
        self.target.run_engine("confirm", tx["token"], ok=False)
        self.target.run_engine("rollback", tx["token"])
        self.target.restored()
        self.assertEqual((self.target.root / "etc/example").read_bytes(), b"original\n")

    def test_staged_rollback_does_not_require_a_backup_and_legacy_content_is_private(self):
        self.target.put("etc/example", b"original")
        token, archive, _ = self.target.bundle({"etc/example": (b"candidate", "600")})
        self.target.run_engine("stage", data=archive)
        response = self.target.run_engine("diff", token, data=b'{"target":"candidate"}')
        self.assertNotIn("etc/example", response["target_contents"])
        self.assertIn("etc/example", response["opaque_paths"])
        response = self.target.run_engine("diff", token, data=b'{"target":"backup"}')
        self.assertEqual(response["current"]["files"]["etc/example"], response["target"]["files"]["etc/example"])
        self.target.run_engine("rollback", token)
        self.assertEqual((self.target.root / "etc/example").read_bytes(), b"original")

    def test_diff_read_failure_cleans_private_workspace(self):
        self.target.put("etc/example", b"original")
        self.target.fault_tool("base64")
        self.target.run_engine("diff", data=b'{"paths":["etc/example"],"services":[],"opaque_paths":[]}', ok=False)
        self.assertEqual(list((self.target.root / "tmp").glob("wrtix-diff.*")), [])
        self.assertFalse((self.target.root / "etc/wrtix/transactions/state").exists())

    def test_secret_provenance_survives_ledger_and_receipt_recovery(self):
        self.target.put("etc/private", b"old-secret")
        compiled = compiler.compile_fleet({"schema_version": 1, "kind": "openwrt-fleet", "devices": {"fixture": {"files": {"/etc/private": {"text": "new-secret"}}}}}, resolve_secrets=True)["fixture"]
        compiled["sensitive_paths"] = ["etc/private"]
        identity = self.target.run_engine("identity")
        compiled.update({k: identity[k] for k in ("engine_sha256", "guard_sha256")})
        baseline = self.target.check(list(compiled["files"]))
        tx, bundle = openwrt.pending_bundle(compiled, baseline, "fixture", 120, {})
        self.target.run_engine("stage", data=bundle)
        self.target.run_engine("apply", tx["token"])
        self.target.ready()
        for side in ("candidate", "backup"):
            response = self.target.run_engine("diff", tx["token"], data=openwrt.canonical({"target": side}))
            self.assertNotIn("etc/private", response["contents"])
            self.assertNotIn("etc/private", response["target_contents"])
            self.assertNotIn("old-secret", json.dumps(response))
            self.assertNotIn("new-secret", json.dumps(response))
        self.target.run_engine("confirm", tx["token"])
        response = self.request(["etc/private"])
        self.assertNotIn("etc/private", response["contents"])
        ledger = json.loads((self.target.root / "etc/wrtix/managed.json").read_bytes())
        self.assertIn("etc/private", ledger["sensitive_paths"])


if __name__ == "__main__":
    unittest.main()
