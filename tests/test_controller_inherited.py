# SPDX-License-Identifier: GPL-2.0-only
# This file was generated entirely by AI.
# It has not been reviewed line by line by a human.
"""Portable inherited regressions for SOPS, SSH and private receipts.

Migrated from the original controller without its firmware or AP fixtures.
All identities and secrets used by these tests are synthetic.
"""
from __future__ import annotations
import copy
import json
import os
import shutil
import shlex
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/nixiz"))
from secret_refs import SecretError, SecretResolver
from compiler import CompileError, compile_fleet
FIXTURE_HASH = "$6$fixture$" + "A" * 86
SENTINEL = "synthetic-private-value-quote'\"-slash\\-end"
def generic_manifest():
    return {"schema_version": 1, "kind": "openwrt-fleet", "devices": {"fixture": {}}}

class CipherFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "secrets.yaml"
        self.path.write_bytes(b"encrypted-fixture-v1")
        self.calls = []
        self.values = {"wifi": {"main": SENTINEL, "guest": "fixture-guest-password"},
                       "root": FIXTURE_HASH, "other": "  whitespace matters  "}

    def ref(self, key="wifi/main"):
        return {"sops": {"file": str(self.path), "key": key}}

    def runner(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, json.dumps(self.values).encode(), b"")

class SecretTests(CipherFixture):
    def test_nonroot_ephemeral_identity_storage_uses_private_runtime_or_tmpfs(self):
        private = Path(self.directory.name) / "runtime"
        private.mkdir(mode=0o700)
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(private)}):
            self.assertEqual(SecretResolver._runtime_directory(), str(private))
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": "relative"}), \
                patch("secret_refs.os.access", side_effect=lambda path, flags: path == "/dev/shm"):
            self.assertEqual(SecretResolver._runtime_directory(), "/dev/shm")
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": "relative"}), \
                patch("secret_refs.os.access", return_value=False), self.assertRaises(SecretError):
            SecretResolver._runtime_directory()

    def test_shared_secret_decrypted_once_across_devices_and_nested_lists(self):
        resolver = SecretResolver(runner=self.runner)
        value = {"main": {"password": self.ref()},
                 "test": [self.ref(), self.ref("wifi/guest")]}
        result = resolver.resolve(value)
        self.assertEqual(result["main"]["password"], SENTINEL)
        self.assertEqual(result["test"], [SENTINEL, "fixture-guest-password"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(value["main"]["password"], self.ref())
        args, kwargs = self.calls[0]
        self.assertIn("decrypt", args)
        self.assertNotIn(SENTINEL, repr(args))
        self.assertNotIn(SENTINEL, repr(kwargs.get("env", {})))
        self.assertEqual(kwargs["input"], b"encrypted-fixture-v1")

    def test_whitespace_in_value_is_not_stripped(self):
        self.assertEqual(SecretResolver(runner=self.runner).resolve(self.ref("other")), "  whitespace matters  ")

    def test_references_can_be_inspected_without_decryption(self):
        refs = SecretResolver(runner=self.runner).references({"x": self.ref()})
        self.assertTrue(refs)
        self.assertIn("wifi/main", json.dumps(refs))
        self.assertNotIn(SENTINEL, json.dumps(refs))
        self.assertFalse(self.calls)

    def test_only_string_key_paths_are_accepted(self):
        for key in (["wifi", "main"], None, 7, True):
            with self.subTest(key=key):
                with self.assertRaises(SecretError):
                    SecretResolver(runner=self.runner).resolve(self.ref(key))

    def test_missing_key_fails_without_disclosing_other_values(self):
        with self.assertRaises(SecretError) as raised:
            SecretResolver(runner=self.runner).resolve(self.ref("wifi/missing"))
        self.assertNotIn(SENTINEL, str(raised.exception))

    def test_sops_failure_does_not_echo_stdout_or_stderr(self):
        def failed(args, **kwargs):
            return subprocess.CompletedProcess(args, 1, SENTINEL.encode(), SENTINEL.encode())
        with self.assertRaises(SecretError) as raised:
            SecretResolver(runner=failed).resolve(self.ref())
        self.assertNotIn(SENTINEL, str(raised.exception))

    def test_ciphertext_rotation_cannot_mix_passwords_within_one_invocation(self):
        resolver = SecretResolver(runner=self.runner)
        self.assertEqual(resolver.resolve(self.ref()), SENTINEL)
        self.path.write_bytes(b"encrypted-fixture-v2")
        self.values["wifi"]["main"] = "fixture-rotated-password"
        self.assertEqual(resolver.resolve(self.ref()), SENTINEL)
        self.assertEqual(len(self.calls), 1)
        resolver.close()
        self.assertEqual(resolver.resolve(self.ref()), "fixture-rotated-password")
        self.assertEqual(len(self.calls), 2)


    def test_combines_age_file_and_multiple_ssh_identities_in_anonymous_fd(self):
        age = Path(self.directory.name) / "age.keys"; age.write_bytes(b"AGE-SECRET-KEY-FILE\n")
        ssh = [Path(self.directory.name) / name for name in ("first", "second")]
        for path in ssh:
            path.write_text("fixture SSH key")
        captured = {}
        def runner(args, **kwargs):
            if args[0] == "ssh-to-age":
                return subprocess.CompletedProcess(args, 0, ("AGE-SECRET-KEY-" + Path(args[-1]).name + "\n").encode(), b"")
            captured["fd"] = kwargs["pass_fds"][0]
            self.assertEqual(os.fstat(captured["fd"]).st_mode & 0o777, 0o600)
            self.assertEqual(Path(kwargs["env"]["SOPS_AGE_KEY_FILE"]).read_bytes(),
                             b"AGE-SECRET-KEY-FILE\nAGE-SECRET-KEY-first\nAGE-SECRET-KEY-second\n")
            return self.runner(args, **kwargs)
        resolver = SecretResolver(runner=runner, key_file=str(age), ssh_key_files=[str(path) for path in ssh])
        self.assertEqual(resolver.resolve(self.ref()), SENTINEL)
        with self.assertRaises(OSError):
            os.fstat(captured["fd"])

    def test_existing_gnupg_home_and_missing_optional_host_keys(self):
        def runner(args, **kwargs):
            self.assertEqual(args[0], "sops")
            self.assertEqual(kwargs["env"]["GNUPGHOME"], "/fixture/gnupg")
            self.assertEqual(kwargs["pass_fds"], ())
            return self.runner(args, **kwargs)
        resolver = SecretResolver(runner=runner, key_file="/nonexistent/optional-age-file",
                                  ssh_key_files=["/nonexistent/optional-ed25519"], gnupg_home="/fixture/gnupg")
        self.assertEqual(resolver.resolve(self.ref()), SENTINEL)
        self.assertEqual(len(self.calls), 1)

    def test_age_success_does_not_import_unneeded_pgp_host_keys(self):
        rsa = Path(self.directory.name) / "rsa"; rsa.write_text("fixture RSA")
        resolver = SecretResolver(runner=self.runner, gnupg_ssh_key_files=[str(rsa)])
        self.assertEqual(resolver.resolve(self.ref()), SENTINEL)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0][0], "sops")

    def test_gnupg_ssh_fallback_always_removes_its_private_keyring(self):
        rsa = Path(self.directory.name) / "rsa"; rsa.write_text("fixture RSA")
        factory = tempfile.TemporaryDirectory
        for failure in (False, True):
            with self.subTest(decrypt_failure=failure):
                homes = []; commands = []
                def temporary(**kwargs):
                    self.assertEqual(kwargs["dir"], SecretResolver._runtime_directory())
                    result = factory(prefix=kwargs["prefix"], dir=self.directory.name)
                    homes.append(Path(result.name)); return result
                def runner(args, **kwargs):
                    commands.append(args)
                    if args[0] == "ssh-to-pgp":
                        return subprocess.CompletedProcess(args, 0, b"-----BEGIN PGP PRIVATE KEY BLOCK-----\nfixture", b"")
                    if args[0] == "gpg":
                        self.assertEqual(kwargs["env"]["GNUPGHOME"], str(homes[0]))
                        self.assertEqual(homes[0].stat().st_mode & 0o777, 0o700)
                        (homes[0] / "fixture-private-key").write_bytes(kwargs["input"])
                        return subprocess.CompletedProcess(args, 0, b"", b"")
                    if args[0] == "gpgconf":
                        return subprocess.CompletedProcess(args, 0, b"", b"")
                    if len([x for x in commands if x[0] == "sops"]) == 1 or failure:
                        return subprocess.CompletedProcess(args, 1, SENTINEL.encode(), SENTINEL.encode())
                    return self.runner(args, **kwargs)
                resolver = SecretResolver(runner=runner, gnupg_ssh_key_files=[str(rsa)])
                with patch("secret_refs.tempfile.TemporaryDirectory", temporary):
                    if failure:
                        with self.assertRaises(SecretError) as raised:
                            resolver.resolve(self.ref())
                        self.assertNotIn(SENTINEL, str(raised.exception))
                    else:
                        self.assertEqual(resolver.resolve(self.ref()), SENTINEL)
                self.assertTrue(homes)
                self.assertTrue(all(not home.exists() for home in homes))
                self.assertEqual(commands[-1][0], "gpgconf")
                self.assertEqual(commands[-1][-2:], ["--kill", "gpg-agent"])

@unittest.skipUnless(all(shutil.which(name) for name in ("sops", "ssh-to-age", "ssh-keygen")), "real SOPS roundtrip requires sops, ssh-to-age and ssh-keygen")
class RealSopsTests(unittest.TestCase):
    def test_real_encryption_roundtrip_and_anonymous_ssh_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = root / "fixture_ssh"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(identity)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            recipient = subprocess.run(["ssh-to-age", "-i", str(identity) + ".pub"], check=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()
            ciphertext = subprocess.run(["sops", "encrypt", "--age", recipient, "--input-type", "json", "--output-type", "json", "/dev/stdin"],
                                        input=json.dumps({"wifi": {"main": SENTINEL}}).encode(), check=True, cwd=root,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
            source = root / "fixture.json"
            source.write_bytes(ciphertext)
            before = set(root.iterdir())
            resolver = SecretResolver(ssh_key_file=str(identity))
            self.assertEqual(resolver.resolve({"sops": {"file": str(source), "key": "wifi/main"}}), SENTINEL)
            resolver.close()
            self.assertEqual(set(root.iterdir()), before)
            self.assertNotIn(SENTINEL.encode(), source.read_bytes())


    def test_yaml_roundtrip_with_combined_age_file_and_multiple_ssh_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); identities = []
            for name in ("age_file", "ssh_unused", "ssh_recipient"):
                path = root / name
                subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)],
                               check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                identities.append(path)
            age = root / "age.keys"
            age.write_bytes(subprocess.check_output(["ssh-to-age", "-private-key", "-i", str(identities[0])]))
            recipient = subprocess.check_output(["ssh-to-age", "-i", str(identities[2]) + ".pub"]).decode().strip()
            source = root / "shared.yaml"
            source.write_bytes(subprocess.run(["sops", "encrypt", "--age", recipient, "--input-type", "yaml", "--output-type", "yaml", "/dev/stdin"],
                                             input=("wifi:\n  main: " + json.dumps(SENTINEL) + "\n").encode(), cwd=root,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout)
            document = generic_manifest()
            document["decryption"] = {"keyFile": str(age), "sshKeyFiles": [str(path) for path in identities[1:]]}
            document["devices"]["fixture"]["files"] = {
                "/etc/fixture.json": {"format": "json", "value": {"password": {"sops": {"file": str(source), "key": "wifi/main"}}}}}
            compiled = compile_fleet(document, resolve_secrets=True)["fixture"]
            self.assertEqual(json.loads(compiled["files"]["etc/fixture.json"]), {"password": SENTINEL})
            self.assertNotIn(SENTINEL.encode(), source.read_bytes())

    @unittest.skipUnless(all(shutil.which(name) for name in ("ssh-to-pgp", "gpg", "gpgconf")), "PGP roundtrip requires ssh-to-pgp and GnuPG")
    def test_pgp_only_yaml_roundtrip_with_temporary_rsa_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); identity = root / "fixture_rsa"
            subprocess.run(["ssh-keygen", "-q", "-t", "rsa", "-b", "2048", "-N", "", "-f", str(identity)],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            public = subprocess.check_output(["ssh-to-pgp", "-i", str(identity)], stderr=subprocess.PIPE)
            public_home = root / "public-only"; public_home.mkdir(mode=0o700)
            env = dict(os.environ, GNUPGHOME=str(public_home))
            try:
                imported = subprocess.run(["gpg", "--batch", "--with-colons", "--import-options", "show-only", "--import"],
                                          input=public, env=env, capture_output=True, check=True).stdout.decode()
                fingerprint = next(line.split(":")[9] for line in imported.splitlines() if line.startswith("fpr:"))
                subprocess.run(["gpg", "--batch", "--quiet", "--import"], input=public, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
                source = root / "shared.yaml"
                source.write_bytes(subprocess.run(["sops", "encrypt", "--pgp", fingerprint, "--input-type", "yaml", "--output-type", "yaml", "/dev/stdin"],
                                                 input=("wifi:\n  main: " + json.dumps(SENTINEL) + "\n").encode(), cwd=root, env=env,
                                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout)
                factory = tempfile.TemporaryDirectory; homes = []
                def runtime_home(**options):
                    self.assertEqual(options["dir"], SecretResolver._runtime_directory())
                    temporary = factory(prefix=options["prefix"], dir=root)
                    homes.append(Path(temporary.name)); return temporary
                with patch.dict(os.environ, {"GNUPGHOME": str(public_home)}), patch("secret_refs.tempfile.TemporaryDirectory", runtime_home):
                    resolver = SecretResolver.from_config({"gnupgSshKeyFiles": [str(identity)]})
                    self.assertEqual(resolver.resolve({"sops": {"file": str(source), "key": "wifi/main"}}), SENTINEL)
                    resolver.close()
                self.assertTrue(homes)
                self.assertTrue(all(not home.exists() for home in homes))
                self.assertNotIn(SENTINEL.encode(), source.read_bytes())
            finally:
                subprocess.run(["gpgconf", "--homedir", str(public_home), "--kill", "gpg-agent"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)

class TransportErrorTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        from openwrt import Transport
        args = SimpleNamespace(host=None, port=None, control_socket=None, jump_host=None, known_hosts=None)
        environment = patch.dict(os.environ, {"NIX_SSHOPTS": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.transport = Transport(args, {}, {"target_host": "192.0.2.20", "ssh_port": 12022}, "fixture")
        self.addCleanup(self.transport.close)

    def failure(self, stderr, returncode=255, action="legacy"):
        from openwrt import DeployError
        result = subprocess.CompletedProcess([], returncode, SENTINEL.encode(), stderr)
        with patch("openwrt.subprocess.run", return_value=result), self.assertRaises(DeployError) as raised:
            if action == "legacy":
                self.transport.agent_preflight()
            else:
                self.transport.call(action)
        text = str(raised.exception)
        self.assertNotIn(SENTINEL, text)
        self.assertIn("root@192.0.2.20:12022", text)
        self.assertIn("exit " + str(returncode), text)
        return text

    def test_known_ssh_failures_are_classified_without_echoing_remote_output(self):
        cases = [
            (b"Host key verification failed.", "SSH host key verification failed"),
            (b"WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!", "SSH host key changed"),
            (b"root@192.0.2.20: Permission denied (publickey,password).", "SSH authentication failed"),
            (b"Too many authentication failures", "SSH authentication failed"),
            (b"connect to host 192.0.2.20 port 12022: Connection refused", "SSH connection refused"),
            (b"connect to host 192.0.2.20 port 12022: Connection timed out", "SSH connection timed out"),
            (b"connect to host 192.0.2.20 port 12022: No route to host", "SSH target is unreachable"),
            (b"Could not resolve hostname example.invalid", "SSH hostname lookup failed"),
            (b"Connection reset by peer", "SSH connection was closed"),
        ]
        for stderr, expected in cases:
            with self.subTest(expected=expected, stderr=stderr):
                text = self.failure(SENTINEL.encode() + b"\n" + stderr + b"\n" + SENTINEL.encode())
                self.assertIn(expected, text)
                self.assertTrue(text.startswith("agent identity preflight: "))

    def test_unrecognized_ssh_and_remote_errors_remain_private(self):
        for status in (1, 255):
            with self.subTest(status=status):
                text = self.failure(SENTINEL.encode(), returncode=status)
                self.assertIn("remote operation failed", text)

    def test_protocol_errors_remain_readable_but_unknown_codes_are_not_echoed(self):
        for code in ("baseline_changed", "installer_transaction_active"):
            with self.subTest(code=code):
                text = self.failure(b"openwrt-deploy: " + code.encode() + b"\n" + SENTINEL.encode(),
                                    returncode=1, action="stage")
                self.assertTrue(text.startswith("stage: "))
                self.assertIn(code, text)
        text = self.failure(b"openwrt-deploy: private_token_value\n" + SENTINEL.encode(), returncode=1)
        self.assertNotIn("private_token_value", text)

    def test_timeout_and_client_start_errors_do_not_echo_exception_details(self):
        from openwrt import DeployError
        cases = [(subprocess.TimeoutExpired([SENTINEL], 60, output=SENTINEL.encode(), stderr=SENTINEL.encode()),
                  "SSH operation timed out"),
                 (OSError(SENTINEL), "SSH client could not be started")]
        for error, expected in cases:
            with self.subTest(expected=expected), patch("openwrt.subprocess.run", side_effect=error):
                with self.assertRaises(DeployError) as raised:
                    self.transport.agent_preflight()
                text = str(raised.exception)
                self.assertIn(expected, text)
                self.assertIn("root@192.0.2.20:12022", text)
                self.assertNotIn(SENTINEL, text)

    def test_invalid_target_port_is_rejected_before_ssh(self):
        from types import SimpleNamespace
        from openwrt import DeployError, Transport
        for port in ("12022", 0, 65536, True):
            with self.subTest(port=port), patch("openwrt.subprocess.run", side_effect=AssertionError("SSH invoked")):
                args = SimpleNamespace(host=None, port=port, control_socket=None, jump_host=None, known_hosts=None)
                # A zero explicit port previously fell back; malformed stored ports must also be rejected.
                with self.assertRaises(DeployError):
                    Transport(args, {}, {"target_host": "192.0.2.20", "ssh_port": port}, "fixture")

class TransportInteractionTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        from openwrt import Transport
        args = SimpleNamespace(host=None, port=None, control_socket=None, jump_host=None, known_hosts=None)
        environment = patch.dict(os.environ, {"NIX_SSHOPTS": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.transport = Transport(args, {}, {"target_host": "192.0.2.20", "ssh_port": 12022}, "fixture")
        self.addCleanup(self.transport.close)

    @staticmethod
    def ssh_options(args):
        # OpenSSH uses the first command-line value for each -o setting.
        result = {}
        for index, value in enumerate(args[:-1]):
            if value == "-o":
                name, _, setting = args[index + 1].partition("=")
                result.setdefault(name.lower(), setting)
        return result

    def invoke(self, interactive, payload=None):
        result = subprocess.CompletedProcess([], 0, b"remote-result", b"")
        def respond(words, **kwargs):
            if "-G" in words:
                return subprocess.CompletedProcess(words, 0, b"port 12022\nuser root\n", b"")
            return result
        with patch("openwrt.interactive_terminal", return_value=interactive), \
                patch("openwrt.subprocess.run", side_effect=respond) as run, \
                patch.dict(os.environ, {"SSH_AUTH_SOCK": "/fixture/existing-agent", "SSH_ASKPASS_REQUIRE": "force"}):
            self.assertEqual(self.transport.run("fixture-command", data=payload), b"remote-result")
        commands = [call for call in run.call_args_list if "-G" not in call.args[0]]
        self.assertEqual(len(commands), 1)
        for call in run.call_args_list:
            if "-G" in call.args[0]:
                self.assertEqual(call.kwargs["input"], b"")
        return commands[0]

    def test_foreground_host_confirmation_preserves_agent_and_configuration_payload(self):
        payload = bytes(range(256)) + SENTINEL.encode()
        call = self.invoke(True, payload)
        options = self.ssh_options(call.args[0])
        self.assertEqual(options["batchmode"], "no")
        self.assertNotIn("passwordauthentication", options)
        self.assertNotIn("preferredauthentications", options)
        self.assertEqual(call.kwargs["input"], payload)
        self.assertEqual(call.kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(call.kwargs["stderr"], subprocess.PIPE)
        self.assertEqual(call.kwargs["env"]["SSH_AUTH_SOCK"], "/fixture/existing-agent")
        self.assertEqual(call.kwargs["env"]["SSH_ASKPASS_REQUIRE"], "never")
        self.assertNotIn(SENTINEL, repr(call.args))
        self.assertNotIn(SENTINEL, repr(call.kwargs["env"]))

    def test_headless_execution_stays_noninteractive_and_cannot_launch_askpass(self):
        call = self.invoke(False)
        options = self.ssh_options(call.args[0])
        self.assertEqual(options["batchmode"], "yes")
        self.assertEqual(call.kwargs["input"], b"")
        self.assertEqual(call.kwargs["env"]["SSH_ASKPASS_REQUIRE"], "never")
        self.assertEqual(call.kwargs["env"]["SSH_AUTH_SOCK"], "/fixture/existing-agent")

    def test_explicit_batch_and_known_hosts_policies_keep_their_priority(self):
        self.transport.args.known_hosts = "/fixture/pinned known_hosts"
        self.transport.manifest = {"deployment": {"sshOptions": ["-o", "BatchMode=yes"]}}
        call = self.invoke(True)
        options = self.ssh_options(call.args[0])
        self.assertEqual(options["batchmode"], "yes")
        self.assertEqual(options["userknownhostsfile"], "/fixture/pinned known_hosts")
        self.assertEqual(options["stricthostkeychecking"], "yes")

    def test_rejected_host_key_never_replays_the_remote_command(self):
        from openwrt import DeployError
        result = subprocess.CompletedProcess([], 255, b"", b"Host key verification failed.\n" + SENTINEL.encode())
        with patch("openwrt.interactive_terminal", return_value=True), \
                patch("openwrt.subprocess.run", return_value=result) as run:
            with self.assertRaises(DeployError) as raised:
                self.transport.run("fixture-write-once", data=SENTINEL.encode())
        run.assert_called_once()
        self.assertNotIn(SENTINEL, str(raised.exception))
        self.assertEqual(run.call_args.kwargs["input"], SENTINEL.encode())

    def test_background_or_missing_controlling_terminal_cannot_enable_prompts(self):
        from openwrt import interactive_terminal
        for terminal_group in (17, 18):
            with self.subTest(terminal_group=terminal_group), \
                    patch("openwrt.os.open", return_value=123), \
                    patch("openwrt.os.tcgetpgrp", return_value=terminal_group), \
                    patch("openwrt.os.getpgrp", return_value=17), \
                    patch("openwrt.os.close") as close:
                self.assertEqual(interactive_terminal(), terminal_group == 17)
                close.assert_called_once_with(123)
        with patch("openwrt.os.open", side_effect=OSError("no controlling terminal")):
            self.assertFalse(interactive_terminal())

class TransportConnectionTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"NIX_SSHOPTS": ""})
        environment.start()
        self.addCleanup(environment.stop)

    def transport(self, target=None, manifest=None, **kwargs):
        from types import SimpleNamespace
        from openwrt import Transport
        args = SimpleNamespace(target_host=target, port=None, control_socket=None, jump_host=None, known_hosts=None)
        for name, value in kwargs.items():
            setattr(args, name, value)
        result = Transport(args, manifest or {}, {"target_host": "192.0.2.20", "ssh_port": 12022}, "fixture")
        self.addCleanup(result.close)
        return result

    def test_commands_reuse_a_private_master_path_and_other_targets_do_not_share_it(self):
        first = self.transport()
        second = self.transport("192.0.2.21")
        result = subprocess.CompletedProcess([], 0, b"result", b"")
        with patch("openwrt.subprocess.run", return_value=result) as run:
            first.run("first-command")
            first.run("second-command", data=b"payload")
            second.run("third-command")
        options = [TransportInteractionTests.ssh_options(call.args[0]) for call in run.call_args_list]
        self.assertEqual(options[0]["controlmaster"], "auto")
        self.assertEqual(options[0]["controlpersist"], "60")
        self.assertEqual(options[0]["controlpath"], options[1]["controlpath"])
        self.assertNotEqual(options[0]["controlpath"], options[2]["controlpath"])
        directory = Path(options[0]["controlpath"]).parent
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(run.call_args_list[1].kwargs["input"], b"payload")
        first.close()
        self.assertFalse(directory.exists())

    def test_exception_closes_owned_master_without_masking_the_original_failure(self):
        transport = self.transport()
        options = TransportInteractionTests.ssh_options(transport._ssh_options())
        directory = Path(options["controlpath"]).parent
        control = directory / "ssh-fixture"
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(control))
            with patch("openwrt.subprocess.run", side_effect=subprocess.TimeoutExpired("ssh", 5)) as run:
                with self.assertRaisesRegex(RuntimeError, "original operation failure"):
                    with transport:
                        raise RuntimeError("original operation failure")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][1:5], ["-S", str(control), "-O", "exit"])
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertFalse(directory.exists())
        transport.close()  # Closing an already closed transport is harmless.

    def test_external_sockets_are_never_closed_or_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            external = Path(directory) / "caller-owned"
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(external))
                for source in ("argument", "environment", "manifest"):
                    with self.subTest(source=source):
                        options = ["-o", "ControlPath=" + str(external)]
                        os.environ["NIX_SSHOPTS"] = "" if source != "environment" else shlex.join(options)
                        transport = self.transport(
                            control_socket=str(external) if source == "argument" else None,
                            manifest={"deployment": {"sshOptions": options}} if source == "manifest" else None)
                        transport._ssh_options()
                        with patch("openwrt.subprocess.run") as run:
                            transport.close()
                        run.assert_not_called()
                        self.assertTrue(external.is_socket())

    def test_reconnect_uses_new_private_socket_and_the_original_host_trust(self):
        os.environ["NIX_SSHOPTS"] = "-S /fixture/environment-master -oStrictHostKeyChecking=no"
        original = self.transport("root@192.0.2.200", control_socket="/fixture/caller-master",
                                  manifest={"deployment": {"sshOptions": ["-o", "ControlPath=/fixture/manifest-master"]}})
        original.host_key_alias = "trusted.setup.example.invalid"
        renewed = original.reconnect("192.0.2.20")
        self.addCleanup(renewed.close)
        options = TransportInteractionTests.ssh_options(renewed._ssh_options())
        self.assertEqual(renewed.host, "192.0.2.20")
        self.assertIsNone(renewed.args.control_socket)
        self.assertEqual(options["hostkeyalias"], "trusted.setup.example.invalid")
        self.assertEqual(options["stricthostkeychecking"], "yes")
        self.assertNotIn("/fixture/", options["controlpath"])
        self.assertEqual(options["controlmaster"], "auto")
        self.assertEqual(Path(options["controlpath"]).parent.stat().st_mode & 0o777, 0o700)

    @unittest.skipUnless(shutil.which("ssh"), "OpenSSH configuration evaluation is required")
    def test_native_ssh_option_precedence_and_effective_receipt_endpoint(self):
        # -G interprets OpenSSH options locally; these documentation addresses are never contacted.
        actual_run = subprocess.run
        cases = (
            (None, {}, "", 12023, "root", "manifest-jump", "/fixture/manifest/socket"),
            (None, {}, "-p 22022 -oUser=agentuser -J environment-jump -S /fixture/environment/socket",
             22022, "agentuser", "environment-jump", "/fixture/environment/socket"),
            ("operator@[2001:db8::20]", {"port": 32022, "jump_host": "cli-jump", "control_socket": "/fixture/cli/socket"},
             "-p 22022 -l agentuser -J environment-jump -S /fixture/environment/socket",
             32022, "operator", "cli-jump", "/fixture/cli/socket"),
        )
        for target, arguments, environment, port, user, jump, control in cases:
            with self.subTest(target=target, environment=environment):
                os.environ["NIX_SSHOPTS"] = environment
                transport = self.transport(target, manifest={"deployment": {
                    "port": 12024,
                    "sshOptions": ["-F", "/dev/null", "-o", "Port=12023", "-J", "manifest-jump",
                                   "-S", "/fixture/manifest/socket", "-i", "/fixture/key with spaces"]}},
                    **arguments)
                options = transport._ssh_options()
                with patch("openwrt.subprocess.run", wraps=actual_run) as run:
                    transport._check_options(options)
                    transport._check_options(options)
                self.assertEqual(run.call_count, 1)
                self.assertEqual((transport.port, transport.user), (port, user))
                self.assertEqual(transport.host_key_alias, "[" + transport.host + "]:" + str(port))
                result = actual_run([*options, "-G", transport.host], input=b"", capture_output=True, check=True)
                values = dict(line.split(" ", 1) for line in result.stdout.decode().splitlines() if " " in line)
                self.assertEqual(values["proxyjump"], jump)
                self.assertEqual(values["controlpath"], control)
                self.assertIn("/fixture/key with spaces", options)
                self.assertEqual(run.call_args.kwargs["input"], b"")

    def test_option_normalization_does_not_reinterpret_option_arguments(self):
        from openwrt import DeployError, ssh_options
        self.assertEqual(ssh_options(["-i", "-J", "-Jjump-host", "-S", "/fixture/socket", "-vv"]),
                         ["-i", "-J", "-o", "ProxyJump=jump-host", "-o", "ControlPath=/fixture/socket", "-v", "-v"])
        for options in (["example.invalid"], ["-J"], ["--unknown-flag"]):
            with self.subTest(options=options), self.assertRaises(DeployError):
                ssh_options(options)

    def test_target_validation_and_legacy_alias_preserve_safe_ssh_destinations(self):
        from openwrt import DeployError
        for value in ("-oProxyCommand=bad", "user@-option", "user name@example.invalid", "example.invalid:12022", "root@[invalid]", "root@[]", "@example.invalid"):
            with self.subTest(value=value), self.assertRaises(DeployError):
                self.transport(value)
        for target, expected in (("root@[2001:db8::20]", "2001:db8::20"),
                                 ("root@[fe80::1%25eth0]", "fe80::1%eth0")):
            transport = self.transport(target)
            self.assertEqual((transport.host, transport.user), (expected, "root"))
        transport = self.transport(host="root@legacy.example.invalid")
        self.assertEqual(transport.host, "legacy.example.invalid")

    def test_malformed_environment_or_configuration_fails_without_uploading_payload(self):
        from openwrt import DeployError
        os.environ["NIX_SSHOPTS"] = "-i 'unterminated"
        with self.assertRaisesRegex(DeployError, "invalid NIX_SSHOPTS quoting"):
            self.transport()
        os.environ["NIX_SSHOPTS"] = "-o Port=invalid"
        transport = self.transport()
        result = subprocess.CompletedProcess([], 255, SENTINEL.encode(), SENTINEL.encode())
        with patch("openwrt.subprocess.run", return_value=result) as run:
            with self.assertRaises(DeployError) as raised:
                transport.run("fixture-write-once", data=SENTINEL.encode())
        run.assert_called_once()
        self.assertIn("-G", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["input"], b"")
        self.assertNotIn(SENTINEL, str(raised.exception))

class TransportStdinTests(unittest.TestCase):
    def test_ssh_without_payload_cannot_consume_calling_shell_input(self):
        import shlex
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_ssh = root / "ssh"
            fake_ssh.write_text(
                "#!" + sys.executable + "\n"
                "import sys\n"
                "sys.stdout.buffer.write(sys.stdin.buffer.read())\n"
            )
            fake_ssh.chmod(0o700)
            setup = (
                "import sys; from types import SimpleNamespace; "
                "sys.path.insert(0, " + repr(str(ROOT / "src/nixiz")) + "); "
                "from openwrt import Transport; "
                "args=SimpleNamespace(host=None,port=None,control_socket=None,jump_host=None,known_hosts=None); "
                "transport=Transport(args,{},"
                "{'target_host':'example.invalid','ssh_port':12022},'fixture'); "
            )
            empty_code = setup + "assert transport.run('synthetic-command', data=None) == b''; transport.close()"
            payload_code = setup + (
                "payload=bytes(range(256)); "
                "assert transport.run('synthetic-command', data=payload) == payload; transport.close()"
            )
            command = shlex.quote(sys.executable)
            script = (
                "set -e\n" + command + " -c " + shlex.quote(empty_code) + "\n"
                "printf '%s\\n' 'after-empty-ssh'\n"
                + command + " -c " + shlex.quote(payload_code) + "\n"
                "printf '%s\\n' 'after-payload-ssh'\n"
            )
            env = os.environ.copy()
            env["PATH"] = str(root) + os.pathsep + env.get("PATH", "")
            env["NIX_SSHOPTS"] = ""
            result = subprocess.run(["bash", "-s"], input=script.encode(), env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(result.stdout, b"after-empty-ssh\nafter-payload-ssh\n")

class ReceiptStoreTests(unittest.TestCase):
    def setUp(self):
        import receipts
        self.store = receipts
        temporary = tempfile.TemporaryDirectory(prefix="openwrt-receipt-store-")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.base = self.work / "state"
        environment = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.base)})
        environment.start()
        self.addCleanup(environment.stop)

    def write_receipt(self, device="fixture", state="applied", days=0, path=None, **fields):
        import time
        path = path or self.store.allocate_receipt(device)
        data = {"schema_version": 1, "device_id": device, "host": "192.0.2.20", "port": 12022,
                "state": state, "token": "a" * 32, "changed_paths": [SENTINEL], "private_fixture": SENTINEL}
        data.update(fields)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(data).encode())
        os.replace(temporary, path)
        when = time.time() - days * 86400
        os.utime(path, (when, when))
        return path

    def test_state_selection_and_missing_store_management_are_readonly(self):
        self.assertEqual(self.store.default_directory(), self.base / "openwrt-rebuild/receipts")
        self.assertEqual(self.store.list_receipts(), [])
        self.assertEqual(self.store.clean_receipts(), {"removed": [], "retained": 0, "skipped": 0})
        self.assertFalse(self.base.exists())
        for value in ("", "relative-state"):
            with self.subTest(value=value), patch.dict(os.environ, {"XDG_STATE_HOME": value}), \
                    patch.object(self.store.Path, "home", return_value=self.work / "home"):
                self.assertEqual(self.store.default_directory(), self.work / "home/.local/state/openwrt-rebuild/receipts")

    def test_allocation_creates_only_private_directories_and_unique_unoccupied_names(self):
        paths = [self.store.allocate_receipt("fixture") for _ in range(4)]
        self.assertEqual(len(set(paths)), 4)
        self.assertTrue(all(path.parent == self.store.default_directory() and not path.exists() for path in paths))
        self.assertEqual(paths[0].parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(paths[0].parent.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse(list(paths[0].parent.glob("*.receipt.json")))
        occupied = self.write_receipt()
        # Even a repeated random suffix cannot overwrite a prior receipt.
        with patch.object(self.store.secrets, "token_hex", return_value="f" * 16):
            first = self.store.allocate_receipt("fixture")
            self.write_receipt(path=first)
            with self.assertRaises(self.store.ReceiptError):
                self.store.allocate_receipt("fixture")
        self.assertEqual(json.loads(occupied.read_bytes())["state"], "applied")
        for device in ("../escape", "bad-name", "", SENTINEL):
            with self.subTest(device=device), self.assertRaises(self.store.ReceiptError):
                self.store.allocate_receipt(device)

    def test_listing_is_public_metadata_only_and_filters_devices(self):
        first = self.write_receipt(state="applied", days=1)
        other = self.write_receipt(device="other", state="future-state", days=2)
        rows = self.store.list_receipts()
        self.assertEqual([row["receipt"] for row in rows], [str(first), str(other)])
        self.assertEqual(set(rows[0]), {"receipt", "device_id", "host", "port", "state", "updated_at", "age_days"})
        self.assertEqual(rows[1]["state"], "unknown")
        self.assertEqual(rows[0]["age_days"], 1)
        self.assertTrue(rows[0]["updated_at"].endswith("+00:00"))
        self.assertEqual(self.store.list_receipts("other"), [rows[1]])
        public = json.dumps(rows)
        self.assertNotIn("a" * 32, public)
        self.assertNotIn(SENTINEL, public)
        self.assertNotIn("changed_paths", public)

    def test_cleanup_keeps_pending_unknown_recent_and_explicit_external_receipts(self):
        old_confirmed = self.write_receipt(state="confirmed", days=60)
        old_rollback = self.write_receipt(state="rolled-back", days=60)
        recent = self.write_receipt(state="confirmed", days=5)
        pending = self.write_receipt(state="applied", days=365)
        unknown = self.write_receipt(state="new-state", days=365)
        other = self.write_receipt(device="other", state="confirmed", days=60)
        external = self.write_receipt(path=self.work / "external.receipt.json", state="confirmed", days=60)
        result = self.store.clean_receipts(device="fixture", older_than_days=30)
        self.assertEqual(set(result["removed"]), {str(old_confirmed), str(old_rollback)})
        self.assertEqual(result["retained"], 3)
        self.assertEqual(result["skipped"], 0)
        self.assertTrue(all(path.exists() for path in (recent, pending, unknown, other, external)))
        self.assertEqual(external.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(str(external), json.dumps(self.store.list_receipts()))

    def test_corrupt_nonprivate_and_symbolic_records_are_preserved(self):
        directory = self.store.allocate_receipt("fixture").parent
        corrupt = directory / "corrupt.receipt.json"
        corrupt.write_bytes(b"not json")
        corrupt.chmod(0o600)
        invalid = [self.write_receipt(state=value, days=365) for value in ({"bad": "state"}, ["confirmed"], None)]
        nonprivate = self.write_receipt(state="confirmed", days=365)
        nonprivate.chmod(0o644)
        external = self.write_receipt(path=self.work / "outside.receipt.json", state="confirmed", days=365)
        link = directory / "symbolic.receipt.json"
        link.symlink_to(external)
        self.assertEqual(self.store.list_receipts(), [])
        result = self.store.clean_receipts(older_than_days=0)
        self.assertEqual(result, {"removed": [], "retained": 0, "skipped": 6})
        self.assertTrue(all(path.exists() for path in (corrupt, *invalid, nonprivate, external, link)))
        self.assertTrue(link.is_symlink())

    def test_unsafe_store_paths_are_rejected_without_changing_permissions(self):
        path = self.store.allocate_receipt("fixture").parent
        path.parent.chmod(0o755)
        with self.assertRaises(self.store.ReceiptError):
            self.store.list_receipts()
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o755)
        path.parent.chmod(0o700)
        original = path.with_name("saved")
        path.rename(original)
        path.symlink_to(original, target_is_directory=True)
        with self.assertRaises(self.store.ReceiptError):
            self.store.clean_receipts()
        self.assertTrue(path.is_symlink())
        self.assertTrue(original.is_dir())

    def test_external_receipt_lock_does_not_create_managed_state(self):
        with self.store.receipt_lock(self.work / "explicit.receipt.json"):
            pass
        self.assertFalse(self.base.exists())
        for days in (-1, True, "30", 10 ** 1000):
            with self.subTest(days=str(days)[:16]), self.assertRaises(self.store.ReceiptError):
                self.store.clean_receipts(older_than_days=days)
        self.assertFalse(self.base.exists())

    def test_cleanup_preserves_an_atomic_writer_that_replaces_its_candidate(self):
        path = self.write_receipt(state="confirmed", days=365)
        original_rename = self.store.os.rename
        replaced = False
        def rename(source, destination, **options):
            nonlocal replaced
            if source == path.name and not replaced:
                replaced = True
                self.write_receipt(path=path, state="applied", days=365, token="b" * 32)
            return original_rename(source, destination, **options)
        with patch.object(self.store.os, "rename", side_effect=rename):
            result = self.store.clean_receipts(older_than_days=30)
        self.assertEqual(result["removed"], [])
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(json.loads(path.read_bytes())["state"], "applied")
        self.assertEqual(json.loads(path.read_bytes())["token"], "b" * 32)
        self.assertEqual(len(self.store.list_receipts()), 1)

    def test_cleanup_serializes_with_managed_receipt_updates(self):
        path = self.write_receipt(state="confirmed", days=365)
        code = ("import json,sys; sys.path.insert(0," + repr(str(ROOT / "src/nixiz")) + "); "
                "import receipts; print('ready',flush=True); print(json.dumps(receipts.clean_receipts(older_than_days=30)))")
        with self.store.receipt_lock(path):
            child = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy())
            self.assertEqual(child.stdout.readline(), b"ready\n")
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    child.wait(timeout=0.2)
                self.write_receipt(path=path, state="applied", days=365)
            except BaseException:
                child.kill()
                child.wait(timeout=3)
                raise
        stdout, stderr = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, stderr.decode())
        self.assertEqual(json.loads(stdout), {"removed": [], "retained": 1, "skipped": 0})
        self.assertTrue(path.exists())

if __name__ == "__main__":
    unittest.main()
