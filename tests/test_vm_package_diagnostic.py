#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Focused runner preserves case failure, cleanup and narrow scope."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent / 'vm'))
import package_acceptance as runner
import failure_snapshot


class PackageDiagnostic(unittest.TestCase):
    def invoke(self, directory, acceptance, capture, stop):
        art = directory / 'art'; art.mkdir()
        (art / 'test.apk').write_bytes(b'synthetic-package')
        (art / 'test.pem').write_text('synthetic-public-key')
        (art / 'packages.adb').write_bytes(b'synthetic-index')
        (art / 'release.json').write_text(json.dumps({
            'package': 'test.apk', 'signing_key': 'test.pem', 'package_version': '1.0.0-r1',
            'package_sha256': hashlib.sha256(b'synthetic-package').hexdigest()}))
        calls = []
        def remote(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, b'original-hashes\n', b'')
        args = ['fixture', '--artifacts', str(art), '--directory', str(directory / 'run'),
                '--image', str(directory / 'image'), '--accel', 'tcg', '--case', 'mac-reboot']
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(sys, 'argv', args))
            stack.enter_context(patch.object(runner.machine, 'prepare'))
            start = stack.enter_context(patch.object(runner.machine, 'start'))
            stack.enter_context(patch.object(runner.machine, 'stop', side_effect=stop))
            stack.enter_context(patch.object(runner.machine, 'ssh_args', return_value=['ssh-fixture']))
            stack.enter_context(patch.object(runner, 'Acceptance', return_value=acceptance))
            stack.enter_context(patch.object(runner.subprocess, 'run', side_effect=remote))
            stack.enter_context(patch.object(failure_snapshot, 'snapshot', side_effect=capture))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            runner.main()
            start.assert_called_once_with(directory / 'run/guest', 22080, 22081, accel='tcg')
        return calls

    def test_snapshot_and_stop_failure_preserve_original_case_exception(self):
        original = RuntimeError('original-case-failure'); order = []
        def case():
            order.append('case'); raise original
        def capture(*args):
            order.append('snapshot'); raise OSError('synthetic-private-value')
        def stop(*args):
            order.append('stop'); raise OSError('cleanup-private-value')
        acceptance = Mock(); acceptance.run_mac_diagnostic.side_effect = case
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with self.assertRaises(RuntimeError) as caught:
                self.invoke(directory, acceptance, capture, stop)
            self.assertIs(caught.exception, original)
            self.assertEqual(order, ['case', 'snapshot', 'stop'])
            cleanup = (directory / 'run/cleanup-error.json').read_text()
            self.assertNotIn('private-value', cleanup)
            self.assertTrue(json.loads(cleanup)['original_failure_preserved'])
        acceptance.run.assert_not_called()

    def test_success_is_not_reproduced_and_skips_other_package_suite(self):
        acceptance = Mock(); acceptance.results = ['focused-case']
        capture = Mock(); stop = Mock()
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            calls = self.invoke(directory, acceptance, capture, stop)
            result = json.loads((directory / 'run/summary.json').read_text())
            self.assertEqual(result['outcome'], 'not-reproduced')
            self.assertFalse(any('fix --reinstall' in argv[-1] for argv in calls))
            self.assertFalse((directory / 'run/failure-snapshot.json').exists())
        acceptance.run_mac_diagnostic.assert_called_once()
        acceptance.run.assert_not_called(); capture.assert_not_called(); stop.assert_called_once()

    def test_cleanup_failure_without_case_failure_is_not_success(self):
        error = OSError('cleanup failed')
        with patch.object(runner.machine, 'stop', side_effect=error):
            with self.assertRaises(OSError) as caught:
                runner.stop_vm(Path('/tmp/synthetic-guest'))
            self.assertIs(caught.exception, error)


if __name__ == '__main__':
    unittest.main()
