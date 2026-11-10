"""Synthetic import, provider and package-controller regressions."""
import argparse
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/wrtix"))
import compiler
import intake
import openwrt
import packages
import rebuild
from secret_refs import SecretError, SecretResolver


def snapshot(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, value in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(value)
            archive.addfile(entry, io.BytesIO(value))
    return output.getvalue()


class ImportTests(unittest.TestCase):
    def test_default_is_plain_and_only_selected_options_are_encrypted(self):
        raw = b"config example named\n option password 'private-value'\n list ports 'lan1'\n list ports 'lan2'\nconfig example\n option flag '1'\n"
        parsed, enabled = intake.read_snapshot(snapshot({"config/demo": raw, "init.d/demo": b"", "rc.d/S50demo": b""}), [])
        args = argparse.Namespace(secret_option=[], target_host="root@router.example", port=2222,
                                  device="router", wrtix_input="github:DictXiong/wrtix", system="x86_64-linux")
        module, flake, private = intake.project_configuration(parsed, enabled, args)
        self.assertIn("private-value", module)
        self.assertIn('"lan1" "lan2"', module)
        self.assertNotIn("secrets.yaml", module)
        self.assertEqual(private, {})
        args.secret_option = ["demo.named.password", "demo.named.ports"]
        module, flake, private = intake.project_configuration(parsed, enabled, args)
        self.assertNotIn("private-value", module)
        self.assertNotIn("lan1", module)
        self.assertIn('secret "uci/demo/named/password"', module)
        self.assertIn('[ (secret "uci/demo/named/ports/0") (secret "uci/demo/named/ports/1") ]', module)
        self.assertIn('"anonymous" = true', module)
        self.assertIn('"ssh_port" = 2222', module)
        self.assertEqual(private, {"uci": {"demo": {"named": {"password": "private-value", "ports": {"0": "lan1", "1": "lan2"}}}}})
        self.assertIn("openwrtConfigurations.imported", flake)
        args.secret_option = ["demo.missing.value"]
        with self.assertRaises(openwrt.DeployError):
            intake.project_configuration(parsed, enabled, args)

    def test_unsafe_archives_and_unrepresentable_uci_fail(self):
        for files in ({"../secret": b""}, {"config/demo": b"option orphan 'x'"},
                      {"config/demo": b"config example\n option x 'multi\nline'"}):
            with self.subTest(files=list(files)), self.assertRaises(openwrt.DeployError):
                intake.read_snapshot(snapshot(files), [])
        with self.assertRaises(openwrt.DeployError):
            intake.read_snapshot(snapshot({"config/demo": b"config example"}), ["missing"])

    def test_import_bypasses_flake_and_encryption_failure_leaves_no_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new"
            argv = ["import", "--target-host", "router.example", "--output", str(output), "--secret-option", "demo.named.key", "--sops-age", "age1fixture"]
            args, recovery = rebuild.parse_args(argv)
            self.assertFalse(recovery)
            transport = Mock()
            transport.__enter__ = Mock(return_value=transport)
            transport.__exit__ = Mock(return_value=False)
            transport._run_stage.return_value = snapshot({"config/demo": b"config example named\n option key 'private-value'"})
            with patch.object(intake, "Transport", return_value=transport), \
                    patch.object(intake.subprocess, "run", return_value=Mock(returncode=1, stdout=b"")) as execute:
                with self.assertRaises(openwrt.DeployError):
                    intake.run(args, MagicMock())
                self.assertFalse(output.exists())
                self.assertNotIn("private-value", repr(execute.call_args.args))
                self.assertIn(b"private-value", execute.call_args.kwargs["input"])

    def test_plain_import_needs_no_sops_and_produces_a_project_and_module(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new"
            args, _ = rebuild.parse_args(["import", "--target-host", "router.example", "--output", str(output)])
            transport = MagicMock()
            transport.__enter__.return_value = transport
            transport._run_stage.return_value = snapshot({"config/demo": b"config example named\n option key 'value'"})
            with patch.object(intake, "Transport", return_value=transport), patch.object(intake.subprocess, "run") as execute:
                intake.run(args, MagicMock())
                execute.assert_not_called()
            self.assertEqual({path.name for path in output.iterdir()}, {"configuration.nix", "flake.nix"})
            self.assertEqual((output / "configuration.nix").stat().st_mode & 0o777, 0o600)
            self.assertNotIn("secrets.yaml", (output / "configuration.nix").read_text())
            self.assertIn("./configuration.nix", (output / "flake.nix").read_text())

    def test_encryption_requires_both_selected_fields_and_a_recipient(self):
        import contextlib
        base = ["import", "--target-host", "router.example", "--output", "./new"]
        for options in (["--secret-option", "demo.named.key"], ["--sops-age", "age1fixture"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                rebuild.parse_args(base + options)

    def test_nix_strings_escape_interpolation(self):
        self.assertIn('\\${', intake.nix_value({"key": "${private}"}))

    def test_anonymous_wireless_section_type_is_representable(self):
        raw = b"config wifi-iface\n option ssid 'example'\n"
        parsed, _ = intake.read_snapshot(snapshot({"config/wireless": raw}), [])
        rendered = compiler.serialize_uci(parsed["wireless"])
        self.assertEqual(compiler.parse_uci(rendered), parsed["wireless"])


class ProviderTests(unittest.TestCase):
    def provider(self, directory, body):
        path = Path(directory) / "provider"
        path.write_text("#!" + sys.executable + "\n" + body)
        path.chmod(0o700)
        return {"fixture": {"executable": str(path), "timeoutSeconds": 1}}

    def test_provider_snapshot_and_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            counter = Path(directory) / "count"
            body = ("import json,sys,pathlib\nrequest=json.load(sys.stdin)\n"
                    + "with pathlib.Path(" + repr(str(counter)) + ").open('a') as f: f.write('x')\n"
                    + "print(json.dumps({'value':'  private-value  '}))\n")
            providers = self.provider(directory, body)
            ref = {"secretRef": {"provider": "fixture", "key": "wifi/main"}}
            fleet = {"schema_version": 1, "kind": "openwrt-fleet", "secretProviders": providers,
                     "devices": {"router": {"uci": {"demo": {"s": {"type": "example", "options": {"key": ref}}}}}}}
            preview = compiler.compile_fleet(fleet)
            self.assertFalse(counter.exists())
            self.assertNotIn("private-value", repr(preview))
            resolver = SecretResolver(providers=providers)
            self.assertEqual(resolver.resolve([ref, ref]), ["  private-value  "] * 2)
            self.assertEqual(counter.read_text(), "x")
            self.assertEqual(json.loads(json.dumps(ref)), ref)
            actual = compiler.compile_fleet(fleet, resolve_secrets=True, resolver=resolver)
            self.assertIn(b"  private-value  ", actual["router"]["files"]["etc/config/demo"])
            resolver.close()
            self.assertEqual(resolver.provider_cache, {})

    def test_invalid_failure_and_timeout_are_value_free(self):
        for body in ("import sys\nsys.stderr.write('private-value');sys.exit(1)\n",
                     "print('{\"value\": 7}')\n", "import time\ntime.sleep(10)\n"):
            with tempfile.TemporaryDirectory() as directory:
                resolver = SecretResolver(providers=self.provider(directory, body))
                with self.assertRaises(SecretError) as raised:
                    resolver.resolve({"secretRef": {"provider": "fixture", "key": "example"}})
                self.assertNotIn("private-value", str(raised.exception))
        with self.assertRaises(SecretError):
            SecretResolver().resolve({"secretRef": {"provider": "undeclared", "key": "example"}})


class PackageTests(unittest.TestCase):
    def test_missing_package_names_and_empty_inventory(self):
        transport = Mock()
        transport._run_stage.return_value = b"iperf3\n"
        self.assertEqual(packages.missing_packages(transport, ["tcpdump", "iperf3"]), ["iperf3"])
        self.assertEqual(packages.missing_packages(transport, []), [])
        transport._run_stage.return_value = b"unknown\n"
        with self.assertRaises(openwrt.DeployError):
            packages.missing_packages(transport, ["iperf3"])

    def test_prepare_wrapper_selects_normal_build_and_no_decryption_action(self):
        args, recovery = rebuild.parse_args(["prepare-packages", "--device", "router", "--flake", ".#site"])
        self.assertFalse(recovery)
        self.assertEqual(rebuild.controller_args(args, args.action), ["prepare-packages", "--device", "router"])

    def test_missing_package_hint_preserves_manifest_device_and_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrapper = root / "bin/openwrt-deploy"
            wrapper.parent.mkdir()
            wrapper.touch()
            args = argparse.Namespace(manifest=str(root / "fleet with spaces.json"),
                                      target_host="admin@router.example", port=2222,
                                      control_socket=str(root / "ssh socket"), jump_host="gateway",
                                      known_hosts=str(root / "keys with ' quotes"),
                                      post_switch_host="later.example", initialize=True, all=True, ttl=60)
            with patch.object(openwrt, "HERE", root / "lib/wrtix"):
                command = shlex.split(openwrt.package_preparation_command(args, "router"))
            self.assertEqual(command, [str(wrapper), "--manifest", args.manifest,
                                      "prepare-packages", "--device", "router",
                                      "--target-host", args.target_host, "--port", "2222",
                                      "--control-socket", args.control_socket, "--jump-host", "gateway",
                                      "--known-hosts", args.known_hosts])
            with patch.object(openwrt, "HERE", root / "checkout/src/wrtix"), \
                    patch.object(openwrt, "load_manifest", return_value={"devices": {"router": {}}}), \
                    patch.object(openwrt, "compile_fleet", return_value={"router": {"packages": ["iperf3"]}}), \
                    patch.object(openwrt, "Transport") as transport, \
                    patch.object(openwrt, "missing_packages", return_value=["iperf3"]):
                with self.assertRaises(openwrt.DeployError) as raised:
                    openwrt.main(["--manifest", args.manifest, "deploy", "--device", "router"])
                message = str(raised.exception)
                self.assertIn("required packages are missing: iperf3", message)
                suggested = shlex.split(message.splitlines()[-1].strip())
                self.assertEqual(suggested, [sys.executable, str(root / "checkout/src/wrtix/openwrt.py"),
                                            "--manifest", args.manifest, "prepare-packages", "--device", "router"])
                transport.return_value.__enter__.return_value.call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
