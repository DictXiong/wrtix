#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Synthetic QMP execution-location, privacy and bounded-failure checks."""
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / 'vm'))
import cpu_localization as cpu

REGISTERS = '''RAX=1122334455667788 RBX=SECRET_SENTINEL RCX=0000000000000000
RIP=ffffffff81000123 RFL=00000046 [---Z-P-] CPL=0 II=0 A20=1 SMM=0 HLT=0
CS =0010 0000000000000000 ffffffff 00af9b00 DPL=0 CS64 [-RA]
CR0=80050033 CR2=1122334455667788 CR3=0000000002e0a000 CR4=000006f0
EFER=0000000000000d01
'''
REAL = '''EIP=0000fff0 EFL=00000002 [-------] CPL=0 II=0 A20=1 SMM=0 HLT=0
CS =f000 ffff0000 0000ffff 00009b00 DPL=0 CS16 [-RA]
CR0=60000010 CR2=00000000 CR3=00000000 CR4=00000000
EFER=0000000000000000
'''
ROM = '''FlatView #0
 AS "memory", root: system
 Root memory region: system
  00000000000e0000-00000000000fffff (prio 1, rom): isa-bios @0000000000020000
  00000000fffc0000-00000000ffffffff (prio 0, rom): pc.bios
  0000000000100000-000000001fffffff (prio 0, ram): SECRET_SENTINEL
'''
WITNESS = {'available':True, 'case':'planned-reboot', 'boot_id':'01234567-89ab-cdef-0123-456789abcdef',
           'kernel':{'release':'6.12.71','build_id':'4ceeb66ca5809741d09b5d5c2b438c1b7ae456ce'},
           'kernel_text':{'start':'0xffffffff81000000','end':'0xffffffff81001000'},
           'symbols_complete':True,
           'symbol_entries':[{'name':'first','address':'0xffffffff81000000','type':'T'},
                             {'name':'__pfx_native_restart','address':'0xffffffff81000100','type':'t'},
                             {'name':'native_restart','address':'0xffffffff81000100','type':'T'},
                             {'name':'next','address':'0xffffffff81000200','type':'T'}],
           'token':'SECRET_SENTINEL'}


class Localization(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wrtix-cpu-')
        self.addCleanup(self.temp.cleanup)
        self.vm = Path(self.temp.name)
        (self.vm/'.wrtix-vm').touch()

    def server(self, dispatch):
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(self.vm/'monitor.sock'))
        listener.listen(1); listener.settimeout(1)
        errors, packets = [], []
        def serve():
            try:
                with listener:
                    connection, _ = listener.accept()
                    with connection, connection.makefile('rwb') as stream:
                        connection.settimeout(1)
                        stream.write(b'{"QMP":{"version":{"private":"SECRET_SENTINEL"}}}\n');stream.flush()
                        while True:
                            line = stream.readline()
                            if not line: break
                            packet = json.loads(line); packets.append(packet)
                            result = dispatch(packet)
                            if result is None: continue
                            response = dict(result);response['id'] = packet['id']
                            stream.write(json.dumps(response).encode()+b'\n');stream.flush()
            except (OSError, TimeoutError): pass
            except BaseException as exc: errors.append(exc)
        thread = threading.Thread(target=serve,daemon=True);thread.start()
        def cleanup():
            listener.close();thread.join(1)
            if errors:raise errors[0]
        self.addCleanup(cleanup)
        return packets

    def normal(self, packet):
        command = packet['execute']
        if command=='qmp_capabilities':return {'return':{}}
        if command=='query-cpus-fast':return {'return':[{'cpu-index':i,'thread-id':100+i} for i in range(2)]}
        self.assertEqual(command,'human-monitor-command')
        arguments=packet['arguments'];operation=arguments['command-line']
        if operation=='info mtree -f':return {'return':ROM}
        if operation=='info roms':return {'return':'name="bios-256k.bin" SECRET_SENTINEL'}
        self.assertEqual(operation,'info registers')
        return {'return':'CPU#'+str(arguments['cpu-index'])+'\n'+REGISTERS.replace('HLT=0','HLT='+str(arguments['cpu-index']))}

    def test_register_allowlist_and_preboot_symbol_match_do_not_claim_identity(self):
        registers=cpu.parse_registers(REGISTERS)
        self.assertTrue(registers['complete'])
        self.assertEqual(registers['fields']['mode'],'long64')
        self.assertNotIn('rax',registers['fields']);self.assertNotIn('cr2',registers['fields'])
        result=cpu.locate(registers['fields'],WITNESS)
        self.assertEqual(result['nearest_symbol'],'native_restart')
        self.assertEqual(result['offset'],'0x0000000000000023')
        self.assertEqual(result['next_symbol_address'],'0xffffffff81000200')
        self.assertFalse(result['boot_continuity_proven'])
        self.assertNotIn('symbol_entries',result)
        self.assertFalse(cpu.locate(registers['fields'],{**WITNESS,'available':False})['available'])
        self.assertNotIn('SECRET_SENTINEL',json.dumps([registers,result,cpu._witness_summary(WITNESS)]))

    def test_reset_PC_uses_hidden_CS_base_and_ROM_requires_nonpaging(self):
        registers=cpu.parse_registers(REAL)
        self.assertEqual(registers['fields']['linear_pc'],'0x00000000fffffff0')
        self.assertEqual(registers['fields']['mode'],'real')
        rom=cpu.parse_rom_ranges(ROM)
        self.assertEqual(rom['ranges'][0]['ROM_file_offset'],'0x0000000000020000')
        self.assertEqual(cpu.locate(registers['fields'],{},rom)['ROM_name'],'pc.bios')
        registers['fields']['paging_enabled']=True
        self.assertFalse(cpu.locate(registers['fields'],{},rom)['available'])
        self.assertNotIn('SECRET_SENTINEL',json.dumps(rom))
        other = ('FlatView #1\n AS "pci-dma", root: pci\n'
                 '  0000000000400000-00000000004fffff (prio 0, rom): pc.bios\n')
        mixed=cpu.parse_rom_ranges(ROM+other)
        self.assertEqual(mixed['ranges'],rom['ranges'])
        self.assertFalse(cpu.parse_rom_ranges(ROM.replace(' AS "memory", root: system\n',''))['available'])
        partial=cpu.parse_registers('RIP=ffffffff81000123\n')
        self.assertFalse(partial['complete']);self.assertNotIn('mode',partial['fields'])
        self.assertIn('cr0',partial['missing_fields'])
        unsupported=cpu.parse_registers("unknown command: 'info registers' SECRET_SENTINEL")
        self.assertEqual(unsupported['reason'],'HMP_registers_unsupported_or_CPU_unavailable')
        self.assertNotIn('SECRET_SENTINEL',json.dumps(unsupported))

    def test_two_all_CPU_samples_are_read_only_mapped_and_filtered(self):
        packets=self.server(self.normal)
        result=cpu.sample(self.vm,time.monotonic()+1,WITNESS,interval_seconds=.01)
        self.assertTrue(result['complete']);self.assertTrue(result['CPU_mapping_unchanged'])
        self.assertEqual(len(result['samples']),2)
        for group in result['samples']:
            self.assertEqual([p['cpu_index'] for p in group['cpus']],[0,1])
            self.assertTrue(group['cpus'][1]['registers']['fields']['halted'])
        self.assertNotIn('SECRET_SENTINEL',json.dumps(result))
        calls=[p for p in packets if p['execute']=='human-monitor-command' and p['arguments']['command-line']=='info registers']
        self.assertEqual([p['arguments']['cpu-index'] for p in calls],[0,1,0,1])
        self.assertTrue(all(p['execute'] in cpu._NORMAL_COMMANDS for p in packets))
        self.assertFalse(result['firmware_identity']['available'])

    def test_unsupported_mapping_is_not_empty_CPU_success_or_raw_error(self):
        def dispatch(packet):
            if packet['execute']=='query-cpus-fast':return {'error':{'class':'CommandNotFound','desc':'SECRET_SENTINEL'}}
            return self.normal(packet)
        self.server(dispatch)
        result=cpu.sample(self.vm,time.monotonic()+.5,WITNESS)
        self.assertEqual(result['reason'],'unsupported')
        self.assertFalse(result['available']);self.assertFalse(result['complete'])
        self.assertNotIn('SECRET_SENTINEL',json.dumps(result))

    def test_wrong_CPU_header_never_binds_registers_to_the_requested_CPU(self):
        def dispatch(packet):
            if packet['execute']=='human-monitor-command' and packet['arguments']['command-line']=='info registers':
                return {'return':'CPU#7\n'+REGISTERS}
            return self.normal(packet)
        self.server(dispatch)
        result=cpu.sample(self.vm,time.monotonic()+.5,WITNESS,interval_seconds=.01)
        self.assertFalse(result['available']);self.assertFalse(result['complete'])
        for group in result['samples']:
            for row in group['cpus']:
                self.assertEqual(row['registers']['reason'],'CPU_reply_identity_missing_or_mismatch')
                self.assertNotIn('fields',row['registers'])

    def test_timeout_retains_first_CPU_but_never_marks_complete(self):
        def dispatch(packet):
            if packet['execute']=='human-monitor-command' and packet['arguments'].get('cpu-index')==1:
                time.sleep(.2);return None
            return self.normal(packet)
        self.server(dispatch)
        started=time.monotonic();result=cpu.sample(self.vm,started+.05,WITNESS)
        self.assertLess(time.monotonic()-started,.25)
        self.assertEqual(result['reason'],'timeout');self.assertFalse(result['complete'])
        self.assertEqual(result['samples'][0]['CPU_count_expected'],2)
        self.assertTrue(result['samples'][0]['cpus'][0]['registers']['complete'])
        self.assertEqual(result['samples'][0]['cpus'][1]['registers']['reason'],'timeout')

    def test_expired_budget_never_opens_channel_and_invalid_map_fails(self):
        with patch.object(cpu.socket,'socket') as opened:
            result=cpu.sample(self.vm,time.monotonic()-.01,WITNESS)
        opened.assert_not_called();self.assertEqual(result['reason'],'budget_exhausted')
        connection=object.__new__(cpu._Connection)
        with self.assertRaises(ValueError):connection.call('human-monitor-command',{'command-line':'stop'})
        with self.assertRaises(cpu._QMPError):cpu._cpus([])
        with self.assertRaises(cpu._QMPError):cpu._cpus([{'cpu-index':0,'thread-id':1},{'cpu-index':0,'thread-id':2}])

    def test_restricted_symbols_and_unreported_BIOS_paths_stay_unknown(self):
        witness={'kernel_text':{'start':'0x0','end':'0x1000'},'symbols':{'first':'0x0'}}
        self.assertFalse(cpu.locate({'pc':'0x123'},witness)['available'])
        result=cpu._rom_identity('name="/root/private/bios.bin" name="bios-256k.bin"')
        self.assertFalse(result['available']);self.assertEqual(result['files'],[])
        self.assertNotIn('/root',json.dumps(result))


if __name__=='__main__':unittest.main()
