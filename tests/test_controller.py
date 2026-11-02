# This file was generated entirely by AI.
# It has not been reviewed line by line by a human.
# SPDX-License-Identifier: GPL-2.0-only
"""Generic producer boundaries and deployment safety regressions."""
import argparse
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/nixiz"))
import compiler
import openwrt
import rebuild
from secret_refs import SecretError

BOOT = "11111111-1111-1111-1111-111111111111"
TOKEN = "a" * 32


def section(value="a", **extra):
    return {"type": "example", "options": {"value": value}, **extra}


def manifest(node=None):
    return {"schema_version": 1, "kind": "openwrt-fleet", "devices": {"node": node or {}}}


def compile_node(node=None, **kwargs):
    return compiler.compile_fleet(manifest(node), **kwargs)["node"]


def connection_args(**kwargs):
    values = dict(port=None, control_socket=None, jump_host=None, known_hosts=None,
                  target_host=None, initialize=False)
    values.update(kwargs)
    return argparse.Namespace(**values)


class ResourceTests(unittest.TestCase):
    def test_unsupported_system_resource_fields_are_not_silently_ignored(self):
        for node in ({"users": {"guest": {}}}, {"users": {"root": {"password": "secret"}}},
                     {"services": {"demo": {"enabled": False}}},
                     {"files": {"/etc/example": {"text": "data", "permissions": "600"}}}):
            with self.subTest(node=node), self.assertRaises(compiler.CompileError):
                compile_node(node)

    def test_minimum_device_has_no_network_or_password_policy(self):
        output = compile_node()
        self.assertEqual(set(output["files"]), {"etc/nixiz/identity.json"})
        self.assertEqual(output["target_host"], "node")
        self.assertEqual(output["ssh_port"], 22)
        self.assertFalse(output["root_password_managed"])
        self.assertIsNone(output["resolver_target"])
        self.assertEqual(output["identity"], {"device_id": "node", "board_names": []})

    def test_preserve_existing_authorization_when_releasing_ownership(self):
        output = compile_node()
        before = {"files": {"etc/dropbear/authorized_keys": "old"}, "modes": {}}
        changed = openwrt.changed_files(output, before, {"files": ["etc/dropbear/authorized_keys"]})
        self.assertNotIn("etc/dropbear/authorized_keys", changed)

    def test_uci_quotes_lists_and_anonymous_order(self):
        node = {"uci": {"demo": {"first": section("a'b", order=2), "second": section([True, 3], anonymous=True, order=1)}}}
        out = compile_node(node)["files"]["etc/config/demo"]
        parsed = compiler.parse_uci(out)
        self.assertEqual(list(parsed.values())[0]["options"]["value"], ["1", "3"])
        self.assertEqual(parsed["first"]["options"]["value"], "a'b")

    def test_resource_sets_merge_disjoint_options(self):
        value = {"resourceSets": {"a": {"uci": {"demo": {"s": section()}}},
                                  "b": {"uci": {"demo": {"s": {"type": "example", "options": {"other": "b"}}}}}}}
        output = compile_node(value)
        options = compiler.parse_uci(output["files"]["etc/config/demo"])["s"]["options"]
        self.assertEqual(options, {"value": "a", "other": "b"})

    def test_resource_conflict_does_not_depend_on_import_order(self):
        sets = {"a": {"uci": {"demo": {"s": section("one")}}}, "b": {"uci": {"demo": {"s": section("two")}}}}
        for value in (sets, dict(reversed(list(sets.items())))):
            with self.assertRaises(compiler.CompileError):
                compile_node({"resourceSets": value})

    def test_exclusive_owner_can_render_but_caller_cannot_extend(self):
        value = {"resourceSets": {"owner": {"uci": {"demo": {"s": section()}}, "ownership": {"exclusivePackages": ["demo"]}}}}
        self.assertIn("etc/config/demo", compile_node(value)["files"])
        value["uci"] = {"demo": {"extra": section()}}
        with self.assertRaises(compiler.CompileError):
            compile_node(value)

    def test_explicit_override_is_checked_after_merge(self):
        value = {"resourceSets": {"owner": {"uci": {"demo": {"s": section()}}}},
                 "overrides": {"uci": {"demo": {"s": {"options": {"value": "new"}}}}}}
        self.assertEqual(compiler.parse_uci(compile_node(value)["files"]["etc/config/demo"])["s"]["options"]["value"], "new")
        value["resourceSets"]["owner"]["ownership"] = {"protectedOptions": {"demo": ["value"]}}
        with self.assertRaises(compiler.CompileError):
            compile_node(value)

    def test_protected_owner_cannot_be_bypassed_by_second_set(self):
        value = {"resourceSets": {"owner": {"uci": {"demo": {"s": section()}}, "ownership": {"protectedOptions": {"demo": ["value"]}}},
                                  "other": {"uci": {"demo": {"s": section("x")}}}}}
        with self.assertRaises(compiler.CompileError):
            compile_node(value)

    def test_required_service_cannot_be_missing(self):
        with self.assertRaises(compiler.CompileError):
            compile_node({"ownership": {"requiredServices": ["demo"]}})

    def test_forbidden_and_guard_services_rejected(self):
        for name, ownership in (("demo", {"forbiddenServices": ["demo"]}), ("nixiz-agent", {})):
            with self.subTest(name=name), self.assertRaises(compiler.CompileError):
                compile_node({"services": {name: {"enable": True}}, "ownership": ownership})

    def test_paths_and_raw_uci_ownership_are_protected(self):
        for path in ("/etc/shadow", "/etc/nixiz/identity.json", "/etc/../tmp/foo", "/etc/init.d/nixiz-agent", "/etc/config/network"):
            with self.subTest(path=path), self.assertRaises(compiler.CompileError):
                compile_node({"files": {path: {"text": "x"}}})

    def test_file_sources_and_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source"
            path.write_bytes(b"abc\x00")
            output = compile_node({"files": {"/etc/raw": {"source": str(path), "mode": "0600"},
                                            "/etc/structured": {"value": {"a": 1}, "format": "json"}}})
            self.assertEqual(output["files"]["etc/raw"], b"abc\x00")
            self.assertEqual(json.loads(output["files"]["etc/structured"]), {"a": 1})

    def test_optional_credentials_and_resolver(self):
        value = {"users": {"root": {"passwordHash": "$6$salt$" + "a" * 86, "authorizedKeys": ["ssh-ed25519 AAAA example"]}},
                 "resolver": {"target": "/tmp/resolv.conf.auto"}}
        output = compile_node(value, resolve_secrets=True)
        self.assertTrue(output["root_password_managed"])
        self.assertEqual(output["files"]["etc/dropbear/authorized_keys"], b"ssh-ed25519 AAAA example\n")
        self.assertEqual(output["resolver_target"], "/tmp/resolv.conf.auto")

    def test_invalid_optional_credentials(self):
        for root in ({"passwordHash": "plaintext"}, {"authorizedKeys": ["one\ntwo"]}):
            with self.subTest(root=list(root)), self.assertRaises(compiler.CompileError):
                compile_node({"users": {"root": root}}, resolve_secrets=True)

    def test_resolver_path_validation(self):
        for target in ("relative", "/tmp/../etc/shadow", "/tmp//resolver"):
            with self.subTest(target=target), self.assertRaises(compiler.CompileError):
                compile_node({"resolver": {"target": target}})

    def test_check_executable_must_be_owned_and_executable(self):
        check = {"phase": "preconfirm", "executable": "/usr/libexec/nixiz-managed/check", "input": {"private": "value"}}
        with self.assertRaises(compiler.CompileError):
            compile_node({"checks": {"check": check}})
        node = {"checks": {"check": check}, "files": {check["executable"]: {"text": "#!/bin/sh\nexit 0\n", "mode": "0700"}}}
        output = compile_node(node)
        self.assertEqual(output["checks"][0]["sha256"], compiler.digest(output["files"][check["executable"][1:]]))
        node["files"][check["executable"]]["mode"] = "0644"
        with self.assertRaises(compiler.CompileError):
            compile_node(node)

    def test_checks_with_duplicate_names_fail(self):
        spec = {"phase": "compile", "executable": "/nix/store/example/bin/check"}
        with self.assertRaises(compiler.CompileError):
            compile_node({"checks": {"same": spec}, "resourceSets": {"a": {"checks": {"same": spec}}}})

    def test_compile_check_gets_final_resources_and_private_input(self):
        node = {"uci": {"demo": {"s": section("old")}}, "overrides": {"uci": {"demo": {"s": {"options": {"value": "new"}}}}},
                "checks": {"semantic": {"phase": "compile", "executable": "/nix/store/example/bin/check", "input": {"private": "secret"}}}}
        process = Mock(returncode=0)
        process.__enter__ = Mock(return_value=process)
        process.__exit__ = Mock(return_value=False)
        with patch.object(compiler.subprocess, "Popen", return_value=process) as popen:
            compile_node(node, resolve_secrets=True)
        payload = json.loads(process.communicate.call_args.args[0])
        self.assertEqual(payload["input"], {"private": "secret"})
        self.assertEqual(payload["resources"]["uci"]["demo"]["s"]["options"]["value"], "new")
        self.assertNotIn("checks", payload["fleet"]["node"])
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_compile_check_failure_is_value_free(self):
        process = Mock(returncode=1)
        process.__enter__ = Mock(return_value=process)
        process.__exit__ = Mock(return_value=False)
        with patch.object(compiler.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(compiler.CompileError, "failed: semantic") as error:
                compile_node({"checks": {"semantic": {"phase": "compile", "executable": "/nix/store/example/check", "input": {"secret": "do-not-print"}}}}, resolve_secrets=True)
            self.assertNotIn("do-not-print", str(error.exception))

    def test_preview_does_not_decrypt_or_execute_checks(self):
        with patch.object(compiler.subprocess, "Popen", side_effect=AssertionError("must not execute")):
            output = compile_node({"uci": {"demo": {"s": section({"sops": {"file": "/missing", "key": "password"}})}},
                                   "checks": {"check": {"phase": "compile", "executable": "/nix/store/missing/check"}}})
        self.assertEqual(len(output["secret_references"]), 1)
        self.assertIn(b"openwrt-preview-password", output["files"]["etc/config/demo"])


class TransportTests(unittest.TestCase):
    def transport(self, **kwargs):
        return openwrt.Transport(connection_args(**kwargs), {}, compile_node(), "node")

    def test_existing_auth_is_used_for_enrollment(self):
        with self.transport(initialize=True) as transport, patch.object(openwrt, "interactive_terminal", return_value=True):
            options = transport._ssh_options()
            self.assertNotIn("PreferredAuthentications=password,publickey", options)
            self.assertIn("BatchMode=no", options)
            self.assertNotIn("PasswordAuthentication=no", options)

    def test_ipv6_and_ssh_aliases(self):
        for host in ("root@[2001:db8::1]", "router-alias", "user@router.example"):
            with self.transport(target_host=host) as transport:
                self.assertNotIn(" ", transport.destination())
        for host in ("bad;command", "-option", "root@host/path", "root@host\ncommand"):
            with self.assertRaises(openwrt.DeployError):
                self.transport(target_host=host)

    def test_no_runtime_upload_or_default_password(self):
        compiled = compile_node()
        contract = {"profile": "nixiz-v1", "identity": {"device_id": "node"},
                    "contract_sha256": compiled["contract_sha256"], "engine_sha256": "a" * 64, "guard_sha256": "b" * 64}
        with self.transport() as transport, patch.object(transport, "run", return_value=json.dumps(contract).encode()) as run:
            transport.ensure_engine()
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0], "sh /usr/libexec/nixiz/agent identity")
            self.assertIsNone(run.call_args.kwargs.get("data"))

    def test_preflight_rejects_wrong_device_and_accepts_explicit_unenrolled(self):
        with self.transport() as transport:
            contract = {"identity": {"device_id": "other"}, "boot_id": BOOT, "contract_sha256": transport.compiled["contract_sha256"], "engine_sha256": "a"*64,"guard_sha256":"b"*64}
            with patch.object(transport, "agent_contract", return_value=contract):
                with self.assertRaises(openwrt.DeployError):
                    transport.agent_preflight()
                contract["identity"]["device_id"] = ""
                self.assertEqual(transport.agent_preflight(allow_initialize=True), contract)

    def test_runtime_hashes_are_bound_to_transaction(self):
        compiled = compile_node()
        compiled.update(engine_sha256="a"*64, guard_sha256="b"*64)
        baseline = {"boot_id": BOOT, "identity": {"device_id": ""}, "files": {}, "modes": {}, "services": {}}
        tx, data = openwrt.pending_bundle(compiled, baseline, "node", 120, {}, initialize=True)
        self.assertEqual(tx["kind"], "nixiz-transaction")
        self.assertNotIn("bridge_mac", tx)
        self.assertFalse(tx["root_password_managed"])
        self.assertEqual(tx["engine_sha256"], "a" * 64)
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            self.assertEqual(json.load(archive.extractfile("manifest.json")), tx)

    def test_receipt_deadline_and_origin_cannot_be_replaced(self):
        receipt = {"token": TOKEN, "device_id": "node", "boot_id": BOOT, "origin_boot_id": BOOT}
        with self.assertRaises(openwrt.DeployError):
            openwrt.transaction_status({"token": TOKEN, "device_id": "node", "origin_boot_id": "different"}, receipt)
        with patch.object(openwrt.time, "time", return_value=100):
            self.assertEqual(openwrt.remaining_rollback_seconds({"rollback_deadline": 110}), 10)
            with self.assertRaises(openwrt.DeployError):
                openwrt.remaining_rollback_seconds({"rollback_deadline": 90})

    def test_reconnect_pins_original_host_key(self):
        with self.transport(target_host="root@router.example") as transport:
            with transport.reconnect("192.0.2.2") as moved:
                options = moved._ssh_options()
                self.assertIn("HostKeyAlias=router.example", options)
                self.assertIn("StrictHostKeyChecking=yes", options)
                self.assertEqual(moved.host, "192.0.2.2")

    def test_unknown_remote_errors_do_not_leak(self):
        self.assertEqual(openwrt.remote_failure(b"password=do-not-print", 1), "remote operation failed")
        self.assertIn("preconfirm_check_failed", openwrt.remote_failure(b"openwrt-deploy: preconfirm_check_failed\n", 1))


class RebuildTests(unittest.TestCase):
    def test_explicit_namespace_requires_no_discovery(self):
        with patch.object(rebuild, "nix_json", side_effect=AssertionError("must not query")):
            self.assertEqual(rebuild.configuration_target(".", "openwrtConfigurations.lab", []), '.#openwrtConfigurations."lab".config')

    def test_ambiguous_name_requires_explicit_namespace(self):
        with patch.object(rebuild, "nix_json", side_effect=[{"url": "path:/example"}, {"nixos": True, "standalone": True}]):
            with self.assertRaisesRegex(rebuild.RebuildError, "ambiguous"):
                rebuild.configuration_target(".", "lab", [])

    def test_standalone_selection(self):
        with patch.object(rebuild, "nix_json", side_effect=[{"url": "path:/example"}, {"nixos": False, "standalone": True}]):
            self.assertEqual(rebuild.configuration_target(".", "lab", []), '.#openwrtConfigurations."lab".config')

    def test_nix_expression_literals_escape_interpolation(self):
        self.assertEqual(rebuild.nix_string('path:/tmp/${x}'), '"path:/tmp/\\${x}"')

    def test_receipt_recovery_never_needs_flake(self):
        args, recovery = rebuild.parse_args(["switch", "--receipt", "/tmp/receipt"])
        self.assertTrue(recovery)
        self.assertIsNone(args.flake)


if __name__ == "__main__":
    unittest.main()
