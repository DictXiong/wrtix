"""Resolve deployment-only SOPS references without persistent plaintext files."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import stat
import tempfile


class SecretError(ValueError):
    """Value-free diagnostic safe for command output."""


def reference(value):
    if not isinstance(value, dict) or set(value) != {"sops"}:
        return None
    ref = value["sops"]
    if not isinstance(ref, dict) or set(ref) - {"file", "key", "format"} or not {"file", "key"} <= set(ref):
        raise SecretError("invalid SOPS reference structure")
    if not isinstance(ref["file"], str) or not ref["file"] or "\x00" in ref["file"]:
        raise SecretError("SOPS reference requires an encrypted file path")
    if not isinstance(ref["key"], str) or not ref["key"] or any(not part for part in ref["key"].split("/")):
        raise SecretError("SOPS key must be a nonempty slash-delimited string")
    if ref.get("format") not in (None, "json", "yaml"):
        raise SecretError("only JSON and YAML SOPS sources are supported")
    return ref


def duplicate_free(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SecretError("duplicate key in secret source")
        result[key] = value
    return result


class SecretResolver:
    def __init__(self, runner=subprocess.run, key_file=None, ssh_key_file=None,
                 ssh_key_files=None, gnupg_home=None, gnupg_ssh_key_files=None):
        if gnupg_home and gnupg_ssh_key_files:
            raise SecretError("select either GnuPG home or SSH-to-PGP identities")
        self.runner = runner
        self.key_file = key_file
        self.ssh_key_files = list(dict.fromkeys(([ssh_key_file] if ssh_key_file else []) + list(ssh_key_files or [])))
        self.gnupg_home = gnupg_home
        self.gnupg_ssh_key_files = list(dict.fromkeys(gnupg_ssh_key_files or []))
        self.cache = {}
        self.cipher_digests = {}

    @classmethod
    def from_config(cls, identity, **options):
        return cls(key_file=identity.get("keyFile"), ssh_key_file=identity.get("sshKeyFile"),
                   ssh_key_files=identity.get("sshKeyFiles"), gnupg_home=identity.get("gnupgHome"),
                   gnupg_ssh_key_files=identity.get("gnupgSshKeyFiles"), **options)

    def _age_identity(self):
        identities = []
        # sops-nix can name host keys that have not been generated yet. Existing
        # identities and the caller's normal SOPS environment remain usable.
        if self.key_file and Path(self.key_file).exists():
            identities.append(Path(self.key_file).read_bytes())
        for path in self.ssh_key_files:
            if not Path(path).exists():
                continue
            converted = self.runner(["ssh-to-age", "-private-key", "-i", path],
                                    input=b"", stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if converted.returncode or not converted.stdout.startswith(b"AGE-SECRET-KEY-"):
                raise SecretError("SSH identity conversion failed")
            identities.append(converted.stdout)
        if not identities:
            return None
        descriptor = os.memfd_create("openwrt-sops-identity", 0)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(os.dup(descriptor), "wb") as output:
                for identity in identities:
                    output.write(identity.rstrip(b"\n") + b"\n")
            os.lseek(descriptor, 0, os.SEEK_SET)
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _decrypt(self, encrypted, format_, env, descriptor):
        return self.runner(["sops", "decrypt", "--input-type", format_, "--output-type", "json", "/dev/stdin"],
                           input=encrypted, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           check=False, env=env, pass_fds=(() if descriptor is None else (descriptor,)))

    @staticmethod
    def _runtime_directory():
        candidate = os.environ.get("XDG_RUNTIME_DIR")
        if candidate and os.path.isabs(candidate):
            try:
                info = os.lstat(candidate)
                if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0:
                    return candidate
            except OSError:
                pass
        # /run is writable for root; ordinary users use the shared tmpfs whose
        # newly created unpredictable directory still has mode 0700.
        for candidate in ("/run", "/dev/shm"):
            if os.path.isdir(candidate) and os.access(candidate, os.W_OK | os.X_OK):
                return candidate
        raise SecretError("private temporary identity storage is unavailable")

    def _pgp_fallback(self, encrypted, format_, env, descriptor):
        paths = [path for path in self.gnupg_ssh_key_files if Path(path).exists()]
        if not paths:
            raise SecretError("SOPS decryption failed")
        # The keyring is ephemeral, private, and under the runtime directory.
        # Decrypted application secrets still travel only through memory/pipes.
        with tempfile.TemporaryDirectory(prefix="openwrt-sops-", dir=self._runtime_directory()) as home:
            os.chmod(home, 0o700)
            private_env = dict(env, GNUPGHOME=home)
            try:
                for path in paths:
                    converted = self.runner(["ssh-to-pgp", "-private-key", "-i", path],
                                            input=b"", stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
                    if converted.returncode or not converted.stdout.startswith(b"-----BEGIN PGP PRIVATE KEY BLOCK-----"):
                        raise SecretError("SSH-to-PGP identity conversion failed")
                    imported = self.runner(["gpg", "--homedir", home, "--batch", "--quiet", "--import"],
                                           input=converted.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           check=False, env=private_env)
                    if imported.returncode:
                        raise SecretError("temporary PGP identity import failed")
                return self._decrypt(encrypted, format_, private_env, descriptor)
            finally:
                self.runner(["gpgconf", "--homedir", home, "--kill", "gpg-agent"],
                            input=b"", stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                            env=private_env)

    @staticmethod
    def references(value):
        result = []
        def walk(node):
            ref = reference(node)
            if ref is not None:
                if ref not in result:
                    result.append(copy.deepcopy(ref))
            elif isinstance(node, dict):
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)
        walk(value)
        return result

    def _document(self, ref):
        path = Path(ref["file"])
        format_ = ref.get("format") or ("json" if path.suffix == ".json" else "yaml")
        cache_key = (str(path.resolve()), format_)
        # One invocation is one secret snapshot. Do not mix a rotated source
        # into later devices after its first value has already been resolved.
        if cache_key in self.cache:
            return self.cache[cache_key]
        try:
            encrypted = path.read_bytes()
        except OSError:
            raise SecretError("encrypted secret source is unavailable") from None
        if not encrypted or len(encrypted) > 8 * 1024 * 1024:
            raise SecretError("encrypted secret source size is invalid")
        env = os.environ.copy()
        descriptor = None
        try:
            descriptor = self._age_identity()
            if descriptor is not None:
                env["SOPS_AGE_KEY_FILE"] = f"/proc/self/fd/{descriptor}"
            if self.gnupg_home:
                env["GNUPGHOME"] = self.gnupg_home
            output = self._decrypt(encrypted, format_, env, descriptor)
            if output.returncode and self.gnupg_ssh_key_files:
                output = self._pgp_fallback(encrypted, format_, env, descriptor)
            if output.returncode:
                raise SecretError("SOPS decryption failed")
            value = json.loads(output.stdout, object_pairs_hook=duplicate_free)
            if not isinstance(value, dict):
                raise SecretError("SOPS source must contain an object")
            self.cache[cache_key] = value
            self.cipher_digests[cache_key] = hashlib.sha256(encrypted).hexdigest()
            return value
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise SecretError("SOPS invocation or decoded document is invalid") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def resolve(self, value):
        ref = reference(value)
        if ref is not None:
            item = self._document(ref)
            for key in ref["key"].split("/"):
                if not isinstance(item, dict) or key not in item:
                    raise SecretError("referenced SOPS key does not exist")
                item = item[key]
            if not isinstance(item, str):
                raise SecretError("referenced SOPS value must be a string")
            return item
        if isinstance(value, dict):
            return {key: self.resolve(child) for key, child in value.items()}
        if isinstance(value, list):
            return [self.resolve(child) for child in value]
        return value

    def close(self):
        self.cache.clear()
        self.cipher_digests.clear()
