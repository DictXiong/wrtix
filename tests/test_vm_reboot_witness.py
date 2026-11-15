#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Pre-lease kernel/boot binding without public symbol or command-line leaks."""
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent / 'vm'))
import reboot_witness as witness

BOOT = '01234567-89ab-cdef-0123-456789abcdef'
SYMBOLS = b'''ffffffffa1000000 T _text
ffffffffa1000000 T _stext
ffffffffa1000010 T native_machine_emergency_restart
ffffffffa1000020 t SECRET_SENTINEL_SYMBOL
ffffffffa1000030 t SECRET_SENTINEL_SYMBOL
ffffffffa1e00000 T _etext
'''
NOTES = struct.pack('<III', 4, 20, 3) + b'GNU\0' + bytes.fromhex(witness.FIXED_KERNEL['build_id'])


def meta(boot=BOOT):
    return ('BOOT '+boot+'\nUPTIME 37\nRELEASE 6.12.71\nCMDLINE console=ttyS0,115200n8 root=PRIVATE password=SECRET_SENTINEL nokaslr reboot=acpi\n').encode()


class RebootWitness(unittest.TestCase):
    def fixture(self, directory, *, end_boot=BOOT, symbols=SYMBOLS):
        (directory / '.wrtix-vm').touch()
        class Acceptance:
            def __init__(self):
                self.directory = directory / 'controller'; self.directory.mkdir()
                self.metas = 0
            def remote(self, command, **kwargs):
                if command == witness.META:
                    self.metas += 1
                    data = meta(end_boot if self.metas == 2 else BOOT)
                elif command == 'cat /sys/kernel/notes': data = NOTES
                elif command == 'cat /proc/kallsyms': data = symbols
                else: raise AssertionError('non-readonly command')
                return subprocess.CompletedProcess([], 0, data, b'')
        return Acceptance()

    def test_exact_build_id_slide_and_duplicate_names_remain_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp); acceptance = self.fixture(directory)
            result = witness.capture(acceptance, directory, 'planned-reboot', BOOT)
            self.assertTrue(result['available'])
            self.assertEqual(result['kaslr_slide'], '0x20000000')
            public = (acceptance.directory / 'reboot-witnesses.json').read_text()
            self.assertNotIn('SECRET_SENTINEL', public)
            self.assertNotIn('symbol_entries', public)
            private = json.loads((directory / 'cpu-witness.private.json').read_text())
            self.assertEqual(len([e for e in private['symbol_entries'] if e['name']=='SECRET_SENTINEL_SYMBOL']), 2)
            self.assertTrue(private['symbols_complete'])
            self.assertEqual((directory / 'cpu-witness.private.json').stat().st_mode & 0o777, 0o600)
            self.assertEqual(result['boot_before']['boot_options'], {'console':['ttyS0,115200n8'],'nokaslr':True,'reboot':['acpi']})

    def test_boot_change_replaces_previous_active_map_with_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp); acceptance=self.fixture(directory,end_boot='11234567-89ab-cdef-0123-456789abcdef')
            (directory/'cpu-witness.private.json').write_text('{"available":true,"symbol_entries":["old"]}')
            result=witness.capture(acceptance,directory,'planned-reboot',BOOT)
            self.assertFalse(result['available'])
            self.assertEqual(result['reason'],'witness_boot_changed')
            active=json.loads((directory/'cpu-witness.private.json').read_text())
            self.assertFalse(active['available']); self.assertNotIn('symbol_entries',active)

    def test_restricted_symbol_addresses_do_not_create_a_valid_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp);acceptance=self.fixture(directory,symbols=b'0000000000000000 T _text\n')
            result=witness.capture(acceptance,directory,'planned-reboot',BOOT)
            self.assertFalse(result['available'])
            self.assertEqual(result['reason'],'symbol_addresses_restricted_or_missing')

    def test_storage_failure_does_not_replace_original_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp);acceptance=self.fixture(directory)
            with patch.object(witness,'_write',side_effect=OSError('PRIVATE')):
                result=witness.capture(acceptance,directory,'planned-reboot',BOOT)
            self.assertFalse(result['available'])
            self.assertEqual(result['reason'],'witness_storage_unavailable')
            self.assertFalse((directory/'cpu-witness.private.json').exists())

    def test_invalid_public_record_is_unavailable_not_a_case_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp);acceptance=self.fixture(directory)
            (acceptance.directory/'reboot-witnesses.json').write_text('[]')
            result=witness.capture(acceptance,directory,'planned-reboot',BOOT)
            self.assertFalse(result['available'])
            self.assertEqual(result['reason'],'witness_storage_unavailable')
            self.assertFalse((directory/'cpu-witness.private.json').exists())

    def test_notes_bounds_and_identity_are_not_guessed(self):
        self.assertEqual(witness.parse_notes(NOTES),witness.FIXED_KERNEL['build_id'])
        with self.assertRaises(ValueError):witness.parse_notes(NOTES[:18])
        with self.assertRaises(ValueError):witness.parse_symbols(b'not a kernel table')

if __name__=='__main__':unittest.main()
