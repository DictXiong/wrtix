#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Normal release gates keep full acceptance and publish only safe diagnostics."""
from pathlib import Path
import shlex
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


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
