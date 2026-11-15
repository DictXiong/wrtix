#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Small read-only QMP/HMP CPU samples for an already failed disposable VM."""
from __future__ import annotations
import bisect
import hashlib
import json
import math
from pathlib import Path
import re
import socket
import time

_MAX_REPLY = 131072
_REQUIRED = {'pc', 'cs_selector', 'cs_base', 'cs_flags', 'cr0', 'cr3', 'cr4', 'efer', 'halted'}
_NORMAL_COMMANDS = {'qmp_capabilities', 'query-cpus-fast', 'human-monitor-command'}
_SYMBOL_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_.$]{0,191}')


class _QMPError(Exception):
    def __init__(self, reason):
        self.reason = reason


def _number(value):
    if type(value) is int:
        return value if 0 <= value <= 0xffffffffffffffff else None
    if isinstance(value, str) and re.fullmatch(r'(?:0x)?[0-9a-fA-F]{1,16}', value):
        return int(value.removeprefix('0x'), 16)
    return None


def _hex(value, width=16):
    return '0x' + format(value, '0' + str(width) + 'x')


def parse_registers(text):
    """Discard the entire register dump except the expressly allowed fields."""
    if not isinstance(text, str) or len(text) > _MAX_REPLY:
        return {'available': False, 'reason': 'invalid_register_reply'}
    if re.search(r'(?im)^\s*(?:unknown command:|command .*not found|invalid CPU index)', text):
        return {'available': False, 'reason': 'HMP_registers_unsupported_or_CPU_unavailable'}
    headers = re.findall(r'^\s*CPU#(\d{1,4})\s*$', text, re.M)
    if len(headers) > 1:
        return {'available': False, 'reason': 'multiple_CPU_register_reply'}
    fields = {}
    pc = re.search(r'\b(?:RIP|EIP)=([0-9a-fA-F]{8,16})(?:\s|$)', text)
    if pc:
        fields['pc'] = _hex(int(pc[1], 16))
    for name in ('CR0', 'CR3', 'CR4', 'EFER'):
        match = re.search(r'\b' + name + r'=([0-9a-fA-F]{8,16})(?:\s|$)', text)
        if match:
            fields[name.lower()] = _hex(int(match[1], 16))
    cs = re.search(r'^\s*CS\s*=([0-9a-fA-F]{4})\s+([0-9a-fA-F]{8,16})\s+'
                   r'[0-9a-fA-F]{8}\s+([0-9a-fA-F]{8})(?:\s|$)', text, re.M)
    if cs:
        fields.update(cs_selector=_hex(int(cs[1], 16), 4), cs_base=_hex(int(cs[2], 16)),
                      cs_flags=_hex(int(cs[3], 16), 8))
    halted = re.search(r'\bHLT=([01])(?:\s|$)', text)
    if halted:
        fields['halted'] = halted[1] == '1'
    cpl = re.search(r'\bCPL=([0-3])(?:\s|$)', text)
    if cpl:
        fields['cpl'] = int(cpl[1])
    if all(name in fields for name in ('cr0', 'efer', 'cs_flags')):
        cr0, efer, flags = (int(fields[name], 16) for name in ('cr0', 'efer', 'cs_flags'))
        fields['paging_enabled'] = bool(cr0 & (1 << 31))
        if not cr0 & 1:
            mode = 'real'
        elif efer & (1 << 10):
            mode = 'long64' if flags & (1 << 21) else 'compatibility'
        else:
            mode = 'protected'
        fields['mode'] = mode
        if 'pc' in fields and 'cs_base' in fields:
            linear = int(fields['pc'], 16)
            if mode != 'long64':
                linear = (linear + int(fields['cs_base'], 16)) & 0xffffffffffffffff
            fields['linear_pc'] = _hex(linear)
    missing = sorted(_REQUIRED - fields.keys())
    return {'available': bool(fields), 'complete': not missing, 'fields': fields,
            **({'reported_cpu_index': int(headers[0])} if len(headers) == 1 else {}),
            **({'reason': 'register_fields_missing', 'missing_fields': missing} if missing else {})}


def parse_rom_ranges(text):
    """Retain BIOS ranges only from the CPU physical ``memory`` FlatView."""
    ranges, memory_view_seen = [], False
    if isinstance(text, str) and len(text) <= _MAX_REPLY:
        pattern = re.compile(r'^\s*([0-9a-fA-F]{8,16})-([0-9a-fA-F]{8,16})\s+'
                             r'\([^\r\n)]*\brom\b[^\r\n)]*\):\s*(isa-bios|pc\.bios)'
                             r'(?:\s+@([0-9a-fA-F]{8,16}))?\s*$')
        view, cpu_memory = None, False
        for line in text.splitlines():
            if line.lstrip().startswith('FlatView #'):
                header = re.fullmatch(r'\s*FlatView #([0-9]{1,8})\s*', line)
                view, cpu_memory = (int(header[1]) if header else None), False
                continue
            if view is not None and re.fullmatch(r'\s*AS "memory", root: system\s*', line):
                cpu_memory, memory_view_seen = True, True
                continue
            match = pattern.fullmatch(line) if cpu_memory else None
            if match is None:
                continue
            start, end = int(match[1], 16), int(match[2], 16)
            if start <= end and len(ranges) < 8:
                row = {'start': _hex(start), 'end_inclusive': _hex(end), 'name': match[3],
                       'flatview_index': view}
                if match[4] is not None:
                    row['ROM_file_offset'] = _hex(int(match[4], 16))
                if row not in ranges:
                    ranges.append(row)
    reason = 'ROM_map_unsupported_or_unparsed' if memory_view_seen else 'CPU_memory_FlatView_unavailable'
    return {'available': bool(ranges), 'source': 'actual_QEMU_CPU_memory_FlatView',
            'address_space': 'memory', 'ranges': ranges,
            **({} if ranges else {'reason': reason})}



def _rom_identity(text):
    """Hash only an actually reported BIOS file under the two permitted roots."""
    records = []
    if isinstance(text, str) and len(text) <= _MAX_REPLY:
        pattern = r'(?:name|file)="(/usr/share/(?:qemu|seabios)/bios[A-Za-z0-9_.-]{0,80}\.bin)"'
        for name in dict.fromkeys(re.findall(pattern, text)):
            if len(records) >= 4:
                break
            try:
                path = Path(name).resolve(strict=True)
                if path.parent not in (Path('/usr/share/qemu'), Path('/usr/share/seabios')) or not path.is_file():
                    continue
                size = path.stat().st_size
                if not 0 < size <= 4 * 1024 * 1024:
                    continue
                with path.open('rb') as stream:
                    data = stream.read(4 * 1024 * 1024 + 1)
                if len(data) != size:
                    continue
                records.append({'filename': path.name, 'size': size,
                                'sha256': hashlib.sha256(data).hexdigest()})
            except OSError:
                continue
    return {'available': bool(records), 'source': 'actual_QEMU_info_roms_and_allowed_host_file',
            'files': records, **({} if records else {'reason': 'reported_BIOS_file_hash_unavailable'})}

def _symbol_entries(witness):
    entries = []
    source = witness.get('symbol_entries')
    if isinstance(source, list):
        for row in source:
            if not isinstance(row, dict) or row.get('type') not in ('t', 'T', 'w', 'W'):
                continue
            address, name = _number(row.get('address')), row.get('name')
            if address and isinstance(name, str) and _SYMBOL_NAME.fullmatch(name):
                entries.append((address, name))
    elif isinstance(witness.get('symbols'), dict):
        for name, value in witness['symbols'].items():
            address = _number(value)
            if address and isinstance(name, str) and _SYMBOL_NAME.fullmatch(name):
                entries.append((address, name))
    return sorted(set(entries))


def locate(fields, witness=None, rom=None):
    """Describe an address match; pre-failure symbols do not prove boot identity."""
    witness = witness if isinstance(witness, dict) else {}
    pc = _number(fields.get('linear_pc', fields.get('pc')))
    if pc is None:
        return {'available': False, 'reason': 'instruction_address_unavailable'}
    if fields.get('paging_enabled') is False and isinstance(rom, dict):
        for row in rom.get('ranges', []):
            start, end = _number(row.get('start')), _number(row.get('end_inclusive'))
            if start is not None and end is not None and start <= pc <= end:
                return {'available': True, 'region': 'observed_firmware_ROM',
                        'ROM_name': row['name'], 'range_start': _hex(start), 'range_end_inclusive': _hex(end),
                        'basis': 'actual_ROM_map_and_nonpaging_linear_PC'}
    if witness.get('available') is not True:
        return {'available': False, 'reason': 'pre_failure_witness_unavailable'}
    text = witness.get('kernel_text', {})
    start, end = _number(text.get('start')), _number(text.get('end'))
    if start is not None and end is not None and 0 < start < end and start <= pc < end:
        result = {'available': True, 'region': 'pre_failure_kernel_text_address_match',
                  'basis': 'pre_failure_boot_runtime_kallsyms', 'boot_continuity_proven': False}
        entries = _symbol_entries(witness)
        addresses = sorted({a for a, _ in entries})
        index = bisect.bisect_right(addresses, pc) - 1
        if index >= 0:
            address = addresses[index]
            aliases = sorted(name for at, name in entries if at == address)
            names = [name for name in aliases if not name.startswith('__pfx_')] or aliases
            result.update(nearest_symbol=names[-1], symbol_address=_hex(address),
                          offset=_hex(pc-address), aliases_at_address=len(aliases),
                          symbol_table_complete=witness.get('symbols_complete') is True)
            if index + 1 < len(addresses):
                result['next_symbol_address'] = _hex(addresses[index+1])
        else:
            result['symbol_match'] = 'unavailable'
        return result
    return {'available': False, 'reason': 'instruction_address_not_in_verified_map'}


def _witness_summary(witness):
    """Publish identity metadata only, not the complete symbol table."""
    case = witness.get('case') if isinstance(witness, dict) else None
    case_metadata = {'case': case} if case in ('planned-reboot', 'restore-mac') else {}
    if not isinstance(witness, dict) or witness.get('available') is not True:
        return {'available': False, 'reason': 'pre_failure_witness_unavailable', **case_metadata}
    result = {'available': True, **case_metadata, 'symbol_basis': 'pre_failure_boot_only', 'boot_continuity_proven': False}
    boot = witness.get('boot_id')
    if isinstance(boot, str) and re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', boot):
        result['boot_id'] = boot
    kernel = witness.get('kernel')
    if isinstance(kernel, dict):
        allowed = {}
        if isinstance(kernel.get('release'), str) and re.fullmatch(r'[A-Za-z0-9._+-]{1,80}', kernel['release']):
            allowed['release'] = kernel['release']
        if isinstance(kernel.get('build_id'), str) and re.fullmatch(r'[0-9a-fA-F]{8,128}', kernel['build_id']):
            allowed['build_id'] = kernel['build_id'].lower()
        result['kernel'] = allowed
    for name in ('kaslr_slide', 'kallsyms_sha256', 'symbol_table_sha256', 'kernel_notes_sha256'):
        value = witness.get(name)
        if name in ('kallsyms_sha256', 'symbol_table_sha256', 'kernel_notes_sha256') and isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value):
            result[name] = value
        elif name == 'kaslr_slide' and _number(value) is not None:
            result[name] = _hex(_number(value))
    result['symbols_complete'] = witness.get('symbols_complete') is True
    return result


class _Connection:
    def __init__(self, directory, deadline):
        self.directory, self.deadline, self.buffer, self.counter = directory, deadline, bytearray(), 0
        self.socket = socket.socket(socket.AF_UNIX)

    def __enter__(self):
        try:
            self._timeout()
            self.socket.connect(str(self.directory / 'monitor.sock'))
            greeting = self._line()
            if not isinstance(greeting, dict) or not isinstance(greeting.get('QMP'), dict):
                raise _QMPError('invalid_QMP_greeting')
            self.call('qmp_capabilities')
            return self
        except BaseException:
            self.socket.close()
            raise

    def __exit__(self, *args):
        self.socket.close()

    def _timeout(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError()
        self.socket.settimeout(left)

    def _line(self):
        while b'\n' not in self.buffer:
            self._timeout()
            chunk = self.socket.recv(4096)
            if not chunk:
                raise EOFError()
            self.buffer.extend(chunk)
            if len(self.buffer) > _MAX_REPLY:
                raise _QMPError('QMP_reply_too_large')
        line, _, tail = self.buffer.partition(b'\n')
        self.buffer[:] = tail
        return json.loads(line)

    def call(self, command, arguments=None):
        if command not in _NORMAL_COMMANDS:
            raise ValueError('not a read-only localization command')
        if command == 'human-monitor-command' and (not isinstance(arguments, dict)
                or set(arguments) - {'command-line', 'cpu-index'}
                or arguments.get('command-line') not in ('info mtree -f', 'info roms', 'info registers')
                or ('cpu-index' in arguments and (type(arguments['cpu-index']) is not int
                    or not 0 <= arguments['cpu-index'] < 4096))):
            raise ValueError('not a read-only localization HMP command')
        self.counter += 1
        ident = str(self.counter)
        packet = {'execute': command, 'id': ident}
        if arguments is not None:
            packet['arguments'] = arguments
        self._timeout()
        self.socket.sendall(json.dumps(packet).encode()+b'\n')
        for _ in range(256):
            response = self._line()
            if not isinstance(response, dict) or response.get('id') != ident:
                continue
            if 'error' in response:
                kind = response['error'].get('class') if isinstance(response['error'], dict) else None
                raise _QMPError('unsupported' if kind in ('CommandNotFound', 'DeviceNotFound') else 'command_failed')
            if 'return' not in response:
                raise _QMPError('invalid_QMP_reply')
            return response['return']
        raise _QMPError('QMP_reply_not_found')


def _cpus(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise _QMPError('CPU_mapping_unavailable_or_invalid')
    result, seen = [], set()
    for entry in value:
        if not isinstance(entry, dict):
            raise _QMPError('CPU_mapping_unavailable_or_invalid')
        index, thread = entry.get('cpu-index'), entry.get('thread-id')
        if type(index) is not int or not 0 <= index < 4096 or index in seen or type(thread) is not int or thread <= 0:
            raise _QMPError('CPU_mapping_unavailable_or_invalid')
        seen.add(index)
        result.append({'cpu_index': index, 'thread_id': thread})
    return sorted(result, key=lambda row: row['cpu_index'])


def sample(directory, deadline, witness=None, *, interval_seconds=.2):
    """Take at most two all-vCPU samples within the caller's absolute deadline."""
    started = time.monotonic()
    report = {'available': False, 'samples': [], 'witness': _witness_summary(witness),
              'semantics': 'read_only_execution_addresses_not_boot_health_or_identity'}
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or type(interval_seconds) not in (int, float) or not 0 <= interval_seconds <= .5):
        return {**report, 'reason': 'invalid_budget'}
    ending = min(deadline, started + 5.5)
    if ending <= started:
        return {**report, 'reason': 'budget_exhausted'}
    directory = Path(directory)
    marker = directory / '.wrtix-vm'
    if not marker.is_file() or marker.is_symlink():
        return {**report, 'reason': 'invalid_disposable_fixture'}
    try:
        with _Connection(directory, ending) as connection:
            try:
                rom = parse_rom_ranges(connection.call('human-monitor-command', {'command-line': 'info mtree -f'}))
            except _QMPError as error:
                rom = {'available': False, 'reason': error.reason, 'ranges': []}
            report['firmware_map'] = rom
            try:
                report['firmware_identity'] = _rom_identity(connection.call(
                    'human-monitor-command', {'command-line': 'info roms'}))
            except _QMPError as error:
                report['firmware_identity'] = {'available': False, 'reason': error.reason}
            for number in range(2):
                if number:
                    left = ending-time.monotonic()
                    if left <= interval_seconds:
                        raise TimeoutError()
                    time.sleep(interval_seconds)
                mapping = _cpus(connection.call('query-cpus-fast'))
                group = {'sample_index': number, 'from_seconds': round(time.monotonic()-started, 3),
                         'CPU_count_expected': len(mapping), 'cpus': []}
                report['samples'].append(group)
                for cpu in mapping:
                    row = {**cpu, 'from_seconds': round(time.monotonic()-started, 3)}
                    group['cpus'].append(row)
                    try:
                        text = connection.call('human-monitor-command', {
                            'command-line': 'info registers', 'cpu-index': cpu['cpu_index']})
                        registers = parse_registers(text)
                        if registers.get('fields') and registers.get('reported_cpu_index') != cpu['cpu_index']:
                            registers = {'available': False, 'reason': 'CPU_reply_identity_missing_or_mismatch'}
                        row['registers'] = registers
                        row['location'] = locate(registers.get('fields', {}), witness, rom)
                    except _QMPError as error:
                        row['registers'] = {'available': False, 'reason': error.reason}
                    except (TimeoutError, socket.timeout):
                        row['registers'] = {'available': False, 'reason': 'timeout'}
                        raise
                    finally:
                        row['until_seconds'] = round(time.monotonic()-started, 3)
                        group['until_seconds'] = row['until_seconds']
            report['available'] = any('pc' in cpu.get('registers', {}).get('fields', {}) for group in report['samples'] for cpu in group['cpus'])
            report['complete'] = all(cpu['registers'].get('complete') is True for group in report['samples'] for cpu in group['cpus'])
            mappings = [[(c['cpu_index'], c['thread_id']) for c in s['cpus']] for s in report['samples']]
            report['CPU_mapping_unchanged'] = mappings[0] == mappings[1]
    except (TimeoutError, socket.timeout):
        report['reason'] = 'timeout'
    except _QMPError as error:
        report['reason'] = error.reason
    except (OSError, EOFError, ValueError, TypeError, AttributeError):
        report['reason'] = 'channel_unavailable_or_invalid'
    report['available'] = any('pc' in cpu.get('registers', {}).get('fields', {}) for group in report['samples'] for cpu in group['cpus'])
    report.setdefault('complete', False)
    report['elapsed_seconds'] = round(time.monotonic()-started, 3)
    return report
