#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Focused failure-only guest observation and safe console boundary checks."""
import json
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / 'vm'))
import failure_snapshot as capture

BOOT = b'01234567-89ab-cdef-0123-456789abcdef'
READBACK = (b'\nWRTIXSNAP boot_id ' + BOOT + b'\nWRTIXSNAP carrier 1\n'
            b'WRTIXSNAP route4 default via 10.0.2.2 dev eth0 metric 10\n'
            b'WRTIXSNAP route4_scan true\nWRTIXSNAP state_dir_scan true\n'
            b'WRTIXSNAP state_dir_present true\nWRTIXSNAP marker applied true\n'
            b'WRTIXSNAP marker token SECRET_SENTINEL\n'
            b'WRTIXSNAP end_boot_id ' + BOOT + b'\nWRTIXSNAP done\n')


class GuestObservation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wrtix-observer-')
        self.addCleanup(self.temp.cleanup)
        self.vm = Path(self.temp.name)
        (self.vm / '.wrtix-vm').touch()

    def console(self, tail=b''):
        (self.vm / 'console.log').write_bytes(b'Linux version synthetic\n' + tail)

    def fixture(self):
        (self.vm / 'machine.json').write_text(json.dumps({'pid':999999999, 'start_ticks':'1',
                                                        'port':22080, 'alternate_port':22081}))
        for name in ('identity', 'known_hosts'):
            (self.vm / name).write_text('synthetic fixture\n')

    def server(self, handler):
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(self.vm / 'serial.sock'))
        listener.listen(1)
        listener.settimeout(1)
        errors = []
        def run():
            try:
                with listener:
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(.8)
                        handler(connection)
            except (TimeoutError, OSError):
                pass
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        def clean():
            listener.close(); thread.join(1)
            if errors: raise errors[0]
        self.addCleanup(clean)

    def receive_script(self, connection):
        data = bytearray()
        while not data.endswith(b' 2>/dev/null\n'):
            data.extend(connection.recv(4096))
        return data

    def test_latest_boot_milestones_are_passive_sourced_and_do_not_leak_lines(self):
        self.console(b'Please press Enter to activate this console.\nreboot: Restarting system\n'
                     b'Linux version next SECRET_SENTINEL\nprocd: - init -\nurngd: v1 started.\n')
        result = capture._console_evidence(self.vm)
        self.assertEqual(result['classification'], 'normal_userspace_evidence')
        self.assertEqual(result['latest_boot_marker']['kind'], 'linux')
        self.assertEqual(result['milestones_since_latest_marker']['procd_init']['count'], 1)
        self.assertNotIn('normal_console', result['milestones_since_latest_marker'])
        self.assertNotIn('SECRET_SENTINEL', json.dumps(result))
        self.assertEqual(result['source'], 'console.log')
        self.assertGreater(result['latest_boot_marker']['offset'], 0)

    def test_live_new_boot_prevents_input_despite_stale_normal_file(self):
        self.console(b'Please press Enter to activate this console.\n')
        sent = []
        def server(connection):
            connection.sendall(b'reboot: Restarting system\nLinux version next\nPress the [f] key and hit [enter] to enter failsafe mode\n')
            try: sent.append(connection.recv(4096))
            except socket.timeout: pass
        self.server(server)
        result = capture._serial(self.vm, time.monotonic()+.25)
        self.assertFalse(result['available'])
        self.assertFalse(result['input_sent'])
        self.assertFalse(any(sent))
        self.assertEqual(result['passive_stream']['classification'], 'boot_evidence_only')

    def test_file_unsafe_or_new_epoch_vetoes_old_normal_stream(self):
        for update in (b'Entering failsafe mode\n',
                       b'reboot: Restarting system\nLinux version next\n'):
            with self.subTest(update=update):
                self.console(b'procd: - init -\nurngd: v1 started.\n')
                sent = []
                def server(connection):
                    connection.sendall(b'Linux version observed\nprocd: - init -\nurngd: v1 started.\n')
                    with (self.vm / 'console.log').open('ab') as stream:
                        stream.write(update)
                    try: sent.append(connection.recv(4096))
                    except socket.timeout: pass
                self.server(server)
                result = capture._serial(self.vm, time.monotonic()+.22)
                self.assertFalse(result['available'])
                self.assertFalse(result['input_sent'])
                self.assertFalse(any(sent))
                self.assertNotIn('fields',result)
                # The server is done after EOF; allow the next subcase to bind.
                (self.vm / 'serial.sock').unlink(missing_ok=True)

    def test_positive_userspace_requires_fresh_prompt_and_boot_bound_readback(self):
        self.console(b'procd: - init -\nurngd: v1 started.\n')
        received = []
        def server(connection):
            self.assertEqual(connection.recv(1), b'\n')
            connection.sendall(b'\nroot@OpenWrt:/# ')
            received.append(self.receive_script(connection))
            connection.sendall(READBACK + b'SECRET_SENTINEL ignored\n')
        self.server(server)
        result = capture._serial(self.vm, time.monotonic()+.7)
        self.assertTrue(result['available'])
        fields = result['fields']
        self.assertTrue(fields['script_boot_consistent'])
        self.assertTrue(fields['carrier'])
        self.assertEqual(fields['routes4'], [{'destination':'0.0.0.0/0','gateway':'10.0.2.2','device':'eth0','metric':10}])
        self.assertEqual(fields['guard_markers'], {'applied':True})
        self.assertNotIn('SECRET_SENTINEL', json.dumps(result))
        self.assertLess(len(received[0]),4000)
        self.assertIn(b'/etc/wrtix/transactions/state', received[0])
        self.assertNotIn(b'cmdline', received[0])

    def test_failsafe_during_prompt_receipt_prevents_script(self):
        self.console(b'Please press Enter to activate this console.\n')
        messages=[]
        def server(connection):
            messages.append(connection.recv(1))
            connection.sendall(b'Entering failsafe mode\nroot@OpenWrt:/# ')
            try: messages.append(connection.recv(4096))
            except socket.timeout: pass
        self.server(server)
        result=capture._serial(self.vm,time.monotonic()+.5)
        self.assertFalse(result['available'])
        self.assertEqual(result['reason'],'boot_changed_or_unsafe_stream')
        self.assertEqual(messages[0],b'\n')
        self.assertFalse(any(b'WRTIXSNAP' in message for message in messages))

    def test_boot_change_during_readback_discards_guest_fields(self):
        self.console(b'Please press Enter to activate this console.\n')
        def server(connection):
            self.assertEqual(connection.recv(1),b'\n')
            connection.sendall(b'\nroot@OpenWrt:/# ')
            self.receive_script(connection)
            with (self.vm/'console.log').open('ab') as stream:stream.write(b'reboot: Restarting system\nLinux version newer\n')
            connection.sendall(READBACK)
        self.server(server)
        result=capture._serial(self.vm,time.monotonic()+.5)
        self.assertFalse(result['available'])
        self.assertNotIn('fields',result)
        self.assertNotIn('partial_fields',result)

    def test_strict_ssh_never_registers_key_and_discards_boot_race(self):
        self.fixture()
        bad = READBACK.replace(b'end_boot_id '+BOOT,b'end_boot_id 11234567-89ab-cdef-0123-456789abcdef')
        response=subprocess.CompletedProcess([],0,bad,b'SECRET_SENTINEL')
        with patch.object(capture.subprocess,'run',return_value=response) as execute:
            result=capture._ssh(self.vm,time.monotonic()+.2)
        argv=execute.call_args.args[0]
        self.assertIn('StrictHostKeyChecking=yes',argv)
        self.assertIn('UpdateHostKeys=no',argv)
        self.assertIn('IdentityAgent=none',argv)
        self.assertNotIn('StrictHostKeyChecking=accept-new',argv)
        self.assertEqual(result['reason'],'guest_boot_unbound')
        self.assertNotIn('fields',result)
        self.assertNotIn('SECRET_SENTINEL',json.dumps(result))

    def test_exhausted_hostfwd_budget_is_not_false_connection_evidence(self):
        self.fixture()
        with patch.object(capture.socket,'socket') as connect:
            result=capture._hostfwd(self.vm,time.monotonic()-.1)
        connect.assert_not_called()
        for entry in result['endpoints'].values():
            self.assertFalse(entry['attempted'])
            self.assertNotIn('tcp_connected',entry)
            self.assertNotIn('ssh_banner_received',entry)

    def test_total_budget_exception_identity_and_timing_labels(self):
        self.console(b'procd: - early -\n')
        def server(connection):
            time.sleep(.2)
        self.server(server)
        original=RuntimeError('original')
        started=time.monotonic()
        with patch.object(capture,'_qmp',return_value={'available':False}):
            with self.assertRaises(RuntimeError) as caught:
                with capture.capture_on_failure(self.vm,self.vm/'result.json',.08):
                    raise original
        self.assertIs(caught.exception,original)
        self.assertLess(time.monotonic()-started,.3)
        result=json.loads((self.vm/'result.json').read_text())
        self.assertFalse(result['serial']['available'])
        self.assertIn('serial',result['observation_intervals_seconds'])
        self.assertIn('not_guest_boot_progress',result['host_process_semantics'])
        self.assertNotIn('capture_error',result)
        with patch.object(capture,'_serial',return_value={'available':False}) as serial:
            capture.snapshot(self.vm,30)
        self.assertTrue(serial.called)
        self.assertIn('capture_error',capture.snapshot(self.vm,31))


if __name__=='__main__':unittest.main()
