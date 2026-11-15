#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""One ordinary-reboot attempt, unmodified deadlines and original-error cleanup."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent/'vm'))
import ordinary_reboot_control as control

BOOT = '01234567-89ab-cdef-0123-456789abcdef'
NEXT = '11234567-89ab-cdef-0123-456789abcdef'
BASE = {'boot_id':BOOT,'MAC':'52:54:00:12:34:56','network_sha256':'1'*64,
        'address':'10.0.2.15','agent':'absent','transaction':'absent'}
STAGED = {**BASE,'network_sha256':'2'*64,'configured_MAC':control.TARGET_MAC}
AFTER = {**STAGED,'boot_id':NEXT,'MAC':control.TARGET_MAC}


def response(value):
    keys={'boot_id':'BOOT','MAC':'MAC','network_sha256':'NETWORK_SHA','address':'ADDRESS',
          'agent':'AGENT','transaction':'TRANSACTION','configured_MAC':'CONFIGURED_MAC'}
    data=''.join(keys[key]+' '+text+'\n' for key,text in value.items())
    return subprocess.CompletedProcess([],0,data.encode(),b'')


class OrdinaryControl(unittest.TestCase):
    def invoke(self, directory, *, failure=None, stop_failure=None):
        vm=directory/'guest';commands=[];reads=iter([BASE,STAGED,AFTER])
        def prepare(*args):
            vm.mkdir();(vm/'.wrtix-vm').touch()
        def start(*args,**kwargs):
            (vm/'machine.json').write_text(json.dumps({'pid':999999999,'start_ticks':'1','port':22080,'alternate_port':22081}))
        def stop(*args):
            if stop_failure:raise stop_failure
            (vm/'machine.json').unlink()
        remote=Mock();remote.directory=directory;remote.vm=vm
        def command(text,**kwargs):
            commands.append((text,kwargs))
            if text==control._READ:return response(next(reads))
            return subprocess.CompletedProcess([],255 if text=='/sbin/reboot' else 0,b'',b'')
        remote.remote.side_effect=command
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(control.machine,'prepare',side_effect=prepare))
            stack.enter_context(patch.object(control.machine,'start',side_effect=start))
            stack.enter_context(patch.object(control.machine,'stop',side_effect=stop))
            stack.enter_context(patch.object(control,'Remote',return_value=remote))
            stack.enter_context(patch.object(control,'_wait_initial'))
            stack.enter_context(patch.object(control.reboot_witness,'capture',return_value={'available':False}))
            snapshot=stack.enter_context(patch('failure_snapshot.snapshot',return_value={'schema_version':2,'test':'bounded'}))
            if failure:stack.enter_context(patch.object(control,'_wait_reboot',side_effect=failure))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            value=control.run(directory.parent/'image.img.gz',directory)
        return value,commands,snapshot

    def test_control_invokes_one_ordinary_reboot_without_agent_packages_or_reload(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)/'control'
            summary,commands,snapshot=self.invoke(directory)
            texts=[text for text,_ in commands]
            self.assertEqual(texts.count('/sbin/reboot'),1)
            self.assertIn(control._STAGE_MAC,texts)
            self.assertFalse(any('apk ' in text or '/etc/init.d/network' in text or 'reload' in text or 'restart' in text for text in texts))
            self.assertEqual(summary['outcome'],'PASS_CONTROL_ONLY')
            self.assertIsNone(summary['transaction_TTL']);self.assertFalse(summary['agent_installed'])
            self.assertEqual(summary['wait_seconds'],240);self.assertTrue(summary['cleanup']['verified'])
            snapshot.assert_not_called()
            with self.assertRaises(FileExistsError):control.run(Path(temp)/'image.img.gz',directory)

    def test_control_intent_is_exclusive_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)
            first=control._claim_reboot(directory,BASE,STAGED)
            with self.assertRaises(FileExistsError):control._claim_reboot(directory,BASE,STAGED)
            self.assertEqual(json.loads((directory/'CONTROL-INTENT.json').read_text()),first)
            self.assertFalse(first['force'])

    def test_original_control_failure_survives_snapshot_and_cleanup_failure(self):
        for stop_failure in (None,OSError('synthetic-stop-error')):
            with self.subTest(stop_failed=stop_failure is not None),tempfile.TemporaryDirectory() as temp:
                directory=Path(temp)/'control';original=RuntimeError('synthetic-original-failure')
                with self.assertRaises(RuntimeError) as caught:
                    self.invoke(directory,failure=original,stop_failure=stop_failure)
                self.assertIs(caught.exception,original)
                summary=json.loads((directory/'summary.json').read_text())
                self.assertEqual(summary['outcome'],'FAIL_CONTROL_ONLY')
                self.assertTrue(summary['cleanup']['original_failure_preserved'])
                self.assertEqual(summary['failure_snapshot_seconds'],30)
                self.assertTrue((directory/'failure-snapshot.json').exists())
                self.assertEqual(summary['cleanup']['verified'],stop_failure is None)

    def test_reboot_wait_stops_at_original240_and_never_reissues_command(self):
        clock=[0.0];remote=Mock();remote.remote.return_value=response(BASE)
        with patch.object(control.time,'monotonic',side_effect=lambda:clock[0]),patch.object(control.time,'sleep',side_effect=lambda delay:clock.__setitem__(0,clock[0]+delay)):
            with self.assertRaisesRegex(RuntimeError,'within_240_seconds'):
                control._wait_reboot(remote,BASE,240)
        self.assertEqual(clock[0],240)
        self.assertTrue(all(call.args[0]==control._READ for call in remote.remote.call_args_list))
        self.assertTrue(all(0 < call.kwargs['timeout'] <= 8 for call in remote.remote.call_args_list))

    def test_remote_is_strict_after_explicit_fixture_initialization(self):
        with tempfile.TemporaryDirectory() as temp:
            vm=Path(temp);(vm/'machine.json').write_text(json.dumps({'port':22080}))
            remote=control.Remote(vm,vm)
            with patch.object(control.subprocess,'run',return_value=subprocess.CompletedProcess([],0,b'',b'')) as run:
                remote.remote('true',initial=True)
                self.assertIn('StrictHostKeyChecking=accept-new',run.call_args.args[0])
                remote.remote(control._READ)
                argv=run.call_args.args[0]
                self.assertIn('StrictHostKeyChecking=yes',argv)
                self.assertIn('IdentityAgent=none',argv)
                self.assertIn('UpdateHostKeys=no',argv)
                self.assertEqual(argv[argv.index('-F')+1],'/dev/null')

    def test_agent_presence_and_unknown_cleanup_are_not_passing_observations(self):
        line=next(line for line in control._READ.splitlines() if line.startswith('configured_mac='))
        empty=subprocess.run(['sh','-c','uci() { return 1; }; '+line+'\necho \'AGENT absent\''],capture_output=True,check=True)
        self.assertEqual(empty.stdout,b'CONFIGURED_MAC \nAGENT absent\n')
        with self.assertRaises(ValueError):control.parse_identity(response({**BASE,'agent':'present'}).stdout)
        with self.assertRaises(ValueError):control.parse_identity(b'BOOT '+BOOT.encode()+b'\n')
        with patch.object(Path,'read_text',side_effect=OSError('synthetic-private-error')):
            self.assertIsNone(control._owned_alive({'pid':123,'start_ticks':'1'}))


if __name__=='__main__':unittest.main()
