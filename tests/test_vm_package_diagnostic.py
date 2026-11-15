#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Focused runner preserves case failure, cleanup and narrow scope."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent / 'vm'))
import package_acceptance as runner
import failure_snapshot


class PackageDiagnostic(unittest.TestCase):
    def invoke(self, directory, acceptance, capture, stop, snapshot_seconds=None, cpu_localization=False,
               *, case="mac-reboot", observe=False):
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
                '--image', str(directory / 'image'), '--accel', 'tcg']
        if case is not None:
            args.extend(['--case', case])
        if observe:
            args.append('--observe-mac-failures')
        if snapshot_seconds is not None:
            args.extend(['--failure-snapshot-seconds', str(snapshot_seconds)])
        if cpu_localization:
            args.append('--cpu-localization')
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

    def test_legacy_extended_flag_does_not_extend_an_unscoped_failure(self):
        original = RuntimeError('synthetic-original-case-failure')
        acceptance = Mock(); acceptance.run_mac_diagnostic.side_effect = original
        capture = Mock(return_value={'capture': 'synthetic'})
        stop = Mock()
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with self.assertRaises(RuntimeError) as caught:
                self.invoke(directory, acceptance, capture, stop, snapshot_seconds=30)
            self.assertIs(caught.exception, original)
            capture.assert_called_once_with(directory / 'run/guest', 7)
        stop.assert_called_once()
        acceptance.run.assert_not_called()

    def test_extended_budget_rejected_for_normal_acceptance(self):
        args = ['fixture', '--artifacts', '/unused', '--directory', '/unused',
                '--failure-snapshot-seconds', '30']
        with patch.object(sys, 'argv', args), contextlib.redirect_stderr(io.StringIO()):
            with patch.object(runner.machine, 'prepare') as prepare:
                with self.assertRaises(SystemExit) as caught:
                    runner.main()
        self.assertEqual(caught.exception.code, 2)
        prepare.assert_not_called()

    def test_failed_witness_does_not_authorize_a_stale_disk_symbol_map(self):
        import reboot_witness
        original = RuntimeError('synthetic-original-failure')
        acceptance = Mock(); capture = Mock(return_value={'synthetic':True}); stop = Mock()
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            def case():
                vm = directory / 'run/guest'; vm.mkdir()
                (vm / 'cpu-witness.private.json').write_text('{"available":true,"symbol_entries":["stale"]}')
                acceptance.reboot_witness('planned-reboot','01234567-89ab-cdef-0123-456789abcdef')
                raise original
            acceptance.run_mac_diagnostic.side_effect = case
            with patch.object(reboot_witness,'capture',return_value={'available':False}):
                with self.assertRaises(RuntimeError) as caught:
                    self.invoke(directory,acceptance,capture,stop,snapshot_seconds=30,cpu_localization=True)
            self.assertIs(caught.exception,original)
            capture.assert_called_once_with(directory / 'run/guest', 7)
        stop.assert_called_once()

    def test_cpu_localization_rejects_unscoped_budget(self):
        args=['fixture','--artifacts','/unused','--directory','/unused','--case','mac-reboot','--cpu-localization']
        with patch.object(sys,'argv',args), contextlib.redirect_stderr(io.StringIO()):
            with patch.object(runner.machine,'prepare') as prepare:
                with self.assertRaises(SystemExit) as caught:runner.main()
        self.assertEqual(caught.exception.code,2);prepare.assert_not_called()

    def test_full_observation_runs_full_suite_and_preserves_failure_over_observer_and_stop(self):
        import reboot_witness
        original = RuntimeError('synthetic-private-original-failure')
        acceptance = Mock()
        order = []
        def full_case():
            order.append('full')
            acceptance.mac_failure_context = {'case': 'restore-mac', 'boot_id': BOOT, 'transaction_started': False}
            acceptance.reboot_witness('restore-mac', BOOT)
            acceptance.mac_failure_context['transaction_started'] = True
            raise original
        def failed_snapshot(*args, **kwargs):
            order.append('snapshot')
            self.assertEqual(args[1], 30)
            self.assertEqual(kwargs['cpu_witness'], {'available': False, 'case': 'restore-mac'})
            raise OSError('private-snapshot-error')
        def failed_stop(*args):
            order.append('stop')
            raise OSError('private-cleanup-error')
        acceptance.run.side_effect = full_case
        with tempfile.TemporaryDirectory() as temp, patch.object(reboot_witness, 'capture', return_value=public_witness()):
            with self.assertRaises(RuntimeError) as caught:
                # No --case: exercise the unchanged default full selection.
                self.invoke(Path(temp), acceptance, failed_snapshot, failed_stop, case=None, observe=True)
            self.assertIs(caught.exception, original)
        acceptance.run.assert_called_once()
        acceptance.run_mac_diagnostic.assert_not_called()
        self.assertEqual(order, ['full', 'snapshot', 'stop'])

    def test_full_without_observer_keeps_seven_second_failure_capture(self):
        acceptance = Mock(); original = RuntimeError('synthetic-original-failure')
        acceptance.run.side_effect = original
        capture = Mock(return_value={})
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(RuntimeError) as caught:
                self.invoke(Path(temp), acceptance, capture, Mock(), case=None)
            self.assertIs(caught.exception, original)
            capture.assert_called_once_with(Path(temp) / 'run/guest', 7)
        acceptance.run.assert_called_once()
        acceptance.run_mac_diagnostic.assert_not_called()

    def test_full_non_mac_failure_after_success_cannot_reuse_old_mac_observation(self):
        import reboot_witness
        acceptance = Mock(); original = RuntimeError('synthetic package-fence failure')
        def full_case():
            acceptance.mac_failure_context = {'case': 'restore-mac', 'boot_id': BOOT, 'transaction_started': False}
            acceptance.reboot_witness('restore-mac', BOOT)
            acceptance.mac_failure_context['transaction_started'] = True
            acceptance.mac_failure_context = None  # completed MAC readback, then package case fails
            raise original
        acceptance.run.side_effect = full_case
        capture = Mock(return_value={})
        with tempfile.TemporaryDirectory() as temp, patch.object(reboot_witness, 'capture', return_value=public_witness()):
            with self.assertRaises(RuntimeError) as caught:
                self.invoke(Path(temp), acceptance, capture, Mock(), case=None, observe=True)
            self.assertIs(caught.exception, original)
            capture.assert_called_once_with(Path(temp) / 'run/guest', 7)
        acceptance.run.assert_called_once()
        acceptance.run_mac_diagnostic.assert_not_called()

    def test_observation_selection_interrupt_cannot_replace_original_failure(self):
        acceptance = Mock(); original = RuntimeError('synthetic original case failure')
        acceptance.run.side_effect = original
        capture = Mock(return_value={})
        with tempfile.TemporaryDirectory() as temp, patch.object(runner, 'failure_observation', side_effect=KeyboardInterrupt()):
            with self.assertRaises(RuntimeError) as caught:
                self.invoke(Path(temp), acceptance, capture, Mock(), case=None, observe=True)
            self.assertIs(caught.exception, original)
            capture.assert_called_once_with(Path(temp) / 'run/guest', 7)

    def test_cleanup_failure_without_case_failure_is_not_success(self):
        error = OSError('cleanup failed')
        with patch.object(runner.machine, 'stop', side_effect=error):
            with self.assertRaises(OSError) as caught:
                runner.stop_vm(Path('/tmp/synthetic-guest'))
            self.assertIs(caught.exception, error)


BOOT = '01234567-89ab-cdef-0123-456789abcdef'
OTHER_BOOT = '11234567-89ab-cdef-0123-456789abcdef'


def public_witness(**updates):
    return {'case': 'restore-mac', 'expected_boot_id': BOOT,
            'boot_before': {'boot_id': BOOT}, 'boot_after': {'boot_id': BOOT},
            'available': False, **updates}


class MACObserver(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vm = Path(self.temp.name)
        self.context = {'case': 'restore-mac', 'boot_id': BOOT, 'transaction_started': False}
        self.acceptance = SimpleNamespace(mac_failure_context=self.context)
        self.reader = Mock()
        reader_patch = patch('reboot_witness.capture', self.reader)
        reader_patch.start()
        self.addCleanup(reader_patch.stop)
        self.record = runner.install_mac_observer(self.acceptance, self.vm, include_cpu=True)

    def capture(self, public, private=None):
        if private is not None:
            (self.vm / 'cpu-witness.private.json').write_text(json.dumps(private))
        self.reader.return_value = public
        self.acceptance.reboot_witness('restore-mac', BOOT)
        self.context['transaction_started'] = True
        return runner.failure_observation(self.acceptance, self.record, enabled=True, include_cpu=True)

    def test_positive_current_boot_without_symbols_still_enables_cpu_observation(self):
        # Old disk contents are never read when fresh capture reports missing symbols.
        budget, cpu = self.capture(public_witness(), {'symbol_entries': ['stale-private-value']})
        self.assertEqual(budget, 30)
        self.assertEqual(cpu, {'available': False, 'case': 'restore-mac'})
        self.assertNotIn('stale-private-value', json.dumps(cpu))
        self.assertEqual(runner.failure_observation(self.acceptance, self.record,
                         enabled=False, include_cpu=True), (7, None))
        self.acceptance.mac_failure_context = None
        self.assertEqual(runner.failure_observation(self.acceptance, self.record,
                         enabled=True, include_cpu=True), (7, None))

    def test_missing_or_changed_boot_and_case_cannot_enable_extended_observation(self):
        for changes in ({'boot_before': {}}, {'boot_after': {'boot_id': OTHER_BOOT}},
                        {'expected_boot_id': OTHER_BOOT}, {'case': 'planned-reboot'},
                        {'reason': 'witness_boot_changed'}):
            with self.subTest(changes=changes):
                self.context['transaction_started'] = False
                self.assertEqual(self.capture(public_witness(**changes)), (7, None))

    def test_mutated_or_replaced_context_cannot_reuse_a_previous_witness(self):
        self.capture(public_witness())
        self.context['boot_id'] = OTHER_BOOT
        self.assertEqual(runner.failure_observation(self.acceptance, self.record,
                         enabled=True, include_cpu=True), (7, None))
        self.context['boot_id'] = BOOT
        self.acceptance.mac_failure_context = dict(self.context)
        self.assertEqual(runner.failure_observation(self.acceptance, self.record,
                         enabled=True, include_cpu=True), (7, None))
        self.acceptance.mac_failure_context = self.context
        self.context['transaction_started'] = False
        self.assertEqual(runner.failure_observation(self.acceptance, self.record,
                         enabled=True, include_cpu=True), (7, None))

    def test_symbol_map_requires_positive_digest_case_and_boot_binding(self):
        public = public_witness(available=True, symbol_table_sha256='a' * 64)
        private = {**public, 'boot_id': BOOT, 'symbol_entries': [], 'token': 'private-value'}
        for changed in ({'case': 'planned-reboot'}, {'boot_id': OTHER_BOOT},
                        {'symbol_table_sha256': 'b' * 64}, {'boot_after': {'boot_id': OTHER_BOOT}}):
            with self.subTest(changed=changed):
                self.context['transaction_started'] = False
                self.assertEqual(self.capture(public, {**private, **changed}), (7, None))
        self.context['transaction_started'] = False
        budget, cpu = self.capture(public, private)
        self.assertEqual(budget, 30)
        self.assertTrue(cpu['available'])
        self.assertEqual(cpu['symbol_table_sha256'], 'a' * 64)
        self.assertNotIn('token', cpu)
        self.assertNotIn('private-value', json.dumps(cpu))

    def test_capture_exception_never_reuses_a_previously_valid_map(self):
        self.capture(public_witness())
        self.context['transaction_started'] = False
        self.reader.side_effect = OSError('private-value')
        self.acceptance.reboot_witness('restore-mac', BOOT)
        self.context['transaction_started'] = True
        self.assertEqual(runner.failure_observation(self.acceptance, self.record,
                         enabled=True, include_cpu=True), (7, None))


if __name__ == '__main__':
    unittest.main()
