#!/usr/bin/env python3
# This file was generated entirely by AI.
# It has not been reviewed line by line by a human.
# SPDX-License-Identifier: GPL-2.0-only
"""Inert source-hook tests; paths and root UID are mapped to a private fixture."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'feed/nixiz-agent/files/lib/upgrade/nixiz.sh'


class BackupHook(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='nixiz-backup-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.put('etc/nixiz/identity.json', '{}')
        self.put('list', '/etc/config/existing\n')
        self.status('absent')
        self.put('usr/libexec/nixiz/agent', '#!/bin/sh\ncat ' + shlex.quote(str(self.root / 'status')) + '\n', 0o755)
        script = SOURCE.read_text()
        # Only literal absolute paths are relocated; policy-relative patterns stay intact.
        script = script.replace('/etc/nixiz/', str(self.root) + '/etc/nixiz/')
        script = script.replace('/usr/libexec/nixiz/agent', str(self.root) + '/usr/libexec/nixiz/agent')
        script = script.replace('current= part', 'current=' + shlex.quote(str(self.root)) + ' part')
        script = script.replace('"/$name"', '"' + str(self.root) + '/$name"')
        script = script.replace(' = 0 ]', ' = ' + str(os.geteuid()) + ' ]')
        self.put('hook.sh', script)

    def put(self, relative, data, mode=0o600):
        path = self.root / relative; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data); path.chmod(mode)

    def status(self, value, **extra):
        self.put('status', json.dumps({'state': value, 'reload_complete': True, **extra}))

    def inventory(self, files):
        self.put('etc/nixiz/managed.json', json.dumps({'schema_version': 1, 'kind': 'nixiz-owned-resources', 'files': files}))

    def run_hook(self, success=True):
        result = subprocess.run(['/bin/sh', '-c', '. "$1"; nixiz_add_managed_conffiles "$2"', 'fixture', str(self.root / 'hook.sh'), str(self.root / 'list')], capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, success, result.stderr)
        return result

    def test_package_hooks_preserve_foreign_lifecycle_fence(self):
        lifecycle = SOURCE.parents[2] / 'usr/libexec/nixiz/package-lifecycle'
        script = lifecycle.read_text().replace('root=/etc/nixiz', 'root=' + shlex.quote(str(self.root / 'etc/nixiz')))
        script = script.replace('PATH=/usr/sbin:/usr/bin:/sbin:/bin', 'PATH=' + shlex.quote(os.environ['PATH']))
        self.put('lifecycle.sh', script)
        self.put('etc/nixiz/package-operation', 'fixture-migration\n')
        for action in ('prepare-commit', 'prepare-install', 'prepare-remove', 'finish-install', 'finish-commit'):
            with self.subTest(action=action):
                result = subprocess.run(['/bin/sh', str(self.root / 'lifecycle.sh'), action], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('foreign_operation_pending', result.stderr)
                self.assertEqual((self.root / 'etc/nixiz/package-operation').read_text(), 'fixture-migration\n')

    def test_pending_first_enrollment_without_ledger_is_rejected(self):
        self.status('staged')
        self.assertIn('transaction_pending', self.run_hook(False).stderr)

    def test_idle_unregistered_device_without_ledger_is_unchanged(self):
        self.run_hook()
        self.assertEqual((self.root / 'list').read_text(), '/etc/config/existing\n')

    def test_managed_inventory_preserves_files_without_transaction_archive(self):
        self.put('etc/config/sample', 'config fixture\n')
        self.inventory(['etc/config/sample'])
        self.run_hook(); self.run_hook()
        rows = (self.root / 'list').read_text().splitlines()
        self.assertEqual(rows.count('/etc/config/sample'), 1)
        self.assertIn('/etc/nixiz/identity.json', rows)
        self.assertIn('/etc/nixiz/managed.json', rows)
        self.assertFalse(any('transactions' in row for row in rows))

    def test_invalid_inventory_is_not_partially_appended(self):
        self.put('etc/config/sample', 'config fixture\n')
        self.inventory(['etc/config/sample', 'etc/shadow'])
        self.run_hook(False)
        self.assertEqual((self.root / 'list').read_text(), '/etc/config/existing\n')

    def test_symlink_and_duplicate_paths_rejected(self):
        self.put('etc/config/sample', 'config fixture\n')
        self.inventory(['etc/config/sample', 'etc/config/sample'])
        self.run_hook(False)
        self.inventory(['etc/config/sample'])
        (self.root / 'etc/config/sample').unlink()
        (self.root / 'etc/config/sample').symlink_to(self.root / 'status')
        self.run_hook(False)

    def test_pending_package_restore_and_incomplete_confirm_rejected(self):
        self.status('confirmed', reload_complete=False)
        self.run_hook(False)
        self.status('rolled-back')
        self.put('etc/nixiz/transactions/state/rollback-incomplete', '')
        self.run_hook(False)
        (self.root / 'etc/nixiz/transactions/state/rollback-incomplete').unlink()
        self.put('etc/nixiz/package-operation', 'install')
        self.run_hook(False)


if __name__ == '__main__':
    unittest.main()
