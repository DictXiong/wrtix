#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Pinned VM boot fixture, complete release gates and safe diagnostics."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests/vm'))
import machine


class DirectKernelFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        data = bytearray(1024)
        data[0x202:0x206] = b'HdrS'
        self.kernel = bytes(data)
        self.pin = {**machine.PIN['kernel'], 'size_bytes': len(data),
                    'sha256': hashlib.sha256(data).hexdigest()}
        self.enterContext(patch.dict(machine.PIN, {'kernel': self.pin}))

    def test_extraction_is_read_only_and_hash_bound(self):
        disk = self.root / 'disk.img'
        raw = bytearray(1536)
        raw[446 + 4] = 0x83
        struct.pack_into('<II', raw, 446 + 8, 1, 2)
        disk.write_bytes(raw)
        def debugfs(argv, **kwargs):
            self.assertEqual(argv, ['debugfs', '-R', 'cat /boot/vmlinuz',
                                    str(self.root / 'boot.ext4')])
            self.assertEqual((self.root / 'boot.ext4').read_bytes(), raw[512:])
            kwargs['stdout'].write(self.kernel)
        with patch.object(machine, 'run', side_effect=debugfs):
            machine.extract_kernel(self.root, disk)
        self.assertEqual(disk.read_bytes(), raw)
        self.assertEqual((self.root / 'vmlinuz').read_bytes(), self.kernel)
        self.assertFalse((self.root / 'boot.ext4').exists())

    def test_bad_image_rejected_before_kernel_extraction(self):
        image = self.root / 'bad.img.gz'
        image.write_bytes(b'not the pinned image')
        with patch.object(machine, 'extract_kernel') as extract:
            with self.assertRaisesRegex(RuntimeError, 'official image checksum mismatch'):
                machine.prepare(self.root / 'guest', image)
        extract.assert_not_called()
        self.assertFalse((self.root / 'guest/disk.img').exists())

    def test_bad_kernel_rejected_before_launch(self):
        (self.root / '.wrtix-vm').touch()
        kernel = self.root / 'vmlinuz'
        for kind in ('size', 'hash', 'header', 'symlink'):
            with self.subTest(kind=kind), patch.object(machine.subprocess, 'Popen') as launch:
                kernel.unlink(missing_ok=True)
                data = bytearray(self.kernel)
                if kind == 'size': data.append(0)
                if kind == 'hash': data[-1] = 1
                if kind == 'header': data[0x202:0x206] = b'none'
                kernel.write_bytes(data)
                pin = dict(self.pin)
                if kind == 'header': pin['sha256'] = hashlib.sha256(data).hexdigest()
                if kind == 'symlink':
                    target = self.root / 'kernel-source'
                    target.write_bytes(self.kernel)
                    kernel.unlink()
                    kernel.symlink_to(target)
                with patch.dict(machine.PIN, {'kernel': pin}):
                    with self.assertRaisesRegex(RuntimeError, 'pinned official kernel'):
                        machine.start(self.root, 22080, 22081, accel='tcg')
                launch.assert_not_called()
                self.assertFalse((self.root / 'machine.json').exists())

    def test_launch_preserves_disk_and_transaction_environment(self):
        (self.root / '.wrtix-vm').touch()
        (self.root / 'vmlinuz').write_bytes(self.kernel)
        disk = self.root / 'disk.img'
        disk.write_bytes(b'unchanged root disk')
        with patch.object(machine.subprocess, 'Popen') as launch, patch.object(machine.time, 'sleep'):
            launch.return_value.poll.return_value = None
            launch.return_value.pid = os.getpid()
            machine.start(self.root, 22080, 22081, accel='tcg')
        args = launch.call_args.args[0]
        def option(name): return args[args.index(name) + 1]
        self.assertEqual(option('-kernel'), str(self.root / 'vmlinuz'))
        self.assertEqual(option('-append'),
                         'root=PARTUUID=83beaead-02 rootwait console=tty1 console=ttyS0,115200n8 noinitrd')
        self.assertEqual((option('-m'), option('-smp'), option('-accel')), ('384', '2', 'tcg'))
        self.assertEqual(option('-drive'), f'file={disk},format=raw,if=virtio')
        self.assertEqual(option('-device'), 'virtio-net-pci,netdev=management')
        self.assertEqual(option('-netdev'),
                         'user,id=management,hostfwd=tcp:127.0.0.1:22080-10.0.2.15:22,'
                         'hostfwd=tcp:127.0.0.1:22081-10.0.2.16:22,'
                         'hostfwd=tcp:127.0.0.2:22080-10.0.2.16:22')
        self.assertEqual(option('-serial'), 'chardev:serial')
        self.assertEqual(option('-qmp'), f'unix:{self.root / "monitor.sock"},server=on,wait=off')
        self.assertFalse(set(args) & {'-initrd', '-no-reboot', '-cpu'})
        self.assertEqual(disk.read_bytes(), b'unchanged root disk')
        recorded = json.loads((self.root / 'machine.json').read_text())
        self.assertEqual(recorded['boot_method'], 'direct-kernel')
        self.assertEqual(recorded['kernel_sha256'], self.pin['sha256'])


class VMWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load((ROOT / '.github/workflows/check.yml').read_text())
        cls.jobs = cls.workflow['jobs']

    def test_normal_VM_uses_current_package_and_complete_suite(self):
        vm = self.jobs['vm']
        self.assertEqual(vm['needs'], 'package')
        downloads = [s for s in vm['steps'] if s.get('uses', '').startswith('actions/download-artifact@')]
        self.assertEqual(downloads[0]['with']['name'], 'openwrt-25.12-apk')
        self.assertNotIn('run-id', downloads[0]['with'])
        commands = [shlex.split(s['run']) for s in vm['steps']
                    if 'tests/vm/package_acceptance.py' in s.get('run', '')]
        self.assertEqual(len(commands), 1)
        args = commands[0]
        self.assertIn('--observe-mac-failures', args)
        self.assertNotIn('--case', args)
        self.assertNotIn('--failure-snapshot-seconds', args)
        self.assertNotIn('--cpu-localization', args)
        self.assertEqual(args[args.index('--artifacts') + 1], 'artifacts')

    def test_release_requires_all_normal_jobs_and_original_tag_gate(self):
        self.assertEqual(set(self.jobs['release']['needs']), {'check', 'package', 'vm'})
        self.assertEqual(self.jobs['release']['if'],
                         "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')")
        for name in ('check', 'package', 'vm'):
            self.assertNotIn('if', self.jobs[name])
        events = self.workflow.get('on', self.workflow.get(True))
        self.assertIn('diagnostic/**', events['push']['branches-ignore'])
        self.assertEqual(events['push']['tags'], ['**'])
        self.assertIn('pull_request', events)
        self.assertIn('workflow_dispatch', events)

    def test_public_failure_artifact_has_only_structured_allowlist(self):
        uploads = [s for s in self.jobs['vm']['steps']
                   if s.get('uses', '').startswith('actions/upload-artifact@') and s.get('if') == 'failure()']
        self.assertEqual(len(uploads), 1)
        prefix = '${{ runner.temp }}/wrtix-vm/'
        paths = uploads[0]['with']['path'].splitlines()
        self.assertTrue(all(p.startswith(prefix) for p in paths))
        self.assertEqual({p[len(prefix):] for p in paths}, {
            'controller/results.json', 'controller/reboot-witnesses.json',
            'controller/failure.json', 'failure-snapshot.json', 'cleanup-error.json',
        })
        for path in paths:
            self.assertNotIn('*', path)
            self.assertNotIn('..', path)
            self.assertTrue(path.endswith('.json'))


if __name__ == '__main__':
    unittest.main()
