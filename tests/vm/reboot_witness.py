#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Read-only, pre-lease symbol witnesses for the fixed disposable x86 guest."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import time

# Derived from the pinned official image, not from a guessed x86 link address.
FIXED_KERNEL = {
    'release': '6.12.71',
    'build_id': '4ceeb66ca5809741d09b5d5c2b438c1b7ae456ce',
    'vmlinuz_sha256': '3e3062c5497c5f81b394ae173e876166a09ccd8c401c26ba7331a71f2b3e15b3',
    'ELF_sha256': '220a1f4029b07a306c54c23870a7ed09e5b8386fc701aa4be9ff3feb682214c2',
    'text_link_address': '0xffffffff81000000',
    'image_sha256': '7f33f26f89a7096aa549ec45aa80e07577a3e69c725772a5fa4b8c18df38aeaa',
}
UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
META = "printf 'BOOT '; cat /proc/sys/kernel/random/boot_id; printf 'UPTIME '; cut -d. -f1 /proc/uptime; printf 'RELEASE '; uname -r; printf 'CMDLINE '; cat /proc/cmdline"


def parse_notes(data):
    if len(data) > 1048576:
        raise ValueError('notes_oversized')
    at = 0
    while at + 12 <= len(data):
        namesz, descsz, kind = struct.unpack_from('<III', data, at)
        at += 12
        if namesz > 256 or descsz > 65536 or at + namesz > len(data):
            raise ValueError('notes_invalid')
        name = data[at:at + namesz].rstrip(b'\0')
        at = (at + namesz + 3) & ~3
        if at + descsz > len(data):
            raise ValueError('notes_invalid')
        desc = data[at:at + descsz]
        at = (at + descsz + 3) & ~3
        if name == b'GNU' and kind == 3 and 8 <= len(desc) <= 64:
            return desc.hex()
    raise ValueError('GNU_build_id_unavailable')


def parse_meta(data):
    result = {}
    for line in data.decode('ascii', 'strict').splitlines():
        key, _, value = line.partition(' ')
        if key == 'BOOT' and UUID.fullmatch(value):
            result['boot_id'] = value
        elif key == 'UPTIME' and re.fullmatch(r'\d{1,12}', value):
            result['uptime_seconds'] = int(value)
        elif key == 'RELEASE' and re.fullmatch(r'[A-Za-z0-9_.+-]{1,64}', value):
            result['release'] = value
        elif key == 'CMDLINE':
            options = result['boot_options'] = {}
            for word in value.split():
                if word == 'nokaslr':
                    options['nokaslr'] = True
                elif re.fullmatch(r'console=(?:tty0|ttyS\d{1,2}(?:,\d{3,7}[neo]?\d?)?)', word):
                    options.setdefault('console', []).append(word.split('=', 1)[1])
                elif word.startswith('reboot='):
                    modes = word.split('=', 1)[1].split(',')
                    if modes and all(v in {'acpi','bios','efi','pci','kbd','triple','force','warm','cold','hard','soft','smp'} or re.fullmatch(r'\d{1,3}', v) for v in modes):
                        options['reboot'] = modes
    if not all(k in result for k in ('boot_id', 'release', 'uptime_seconds')):
        raise ValueError('boot_metadata_invalid')
    return result


def parse_symbols(data):
    if not data or len(data) > 8 * 1024 * 1024:
        raise ValueError('symbol_table_invalid')
    entries, named, complete = [], {}, True
    for line in data.decode('ascii', 'strict').splitlines():
        parts = line.split()
        if (len(parts) < 3 or not re.fullmatch(r'[0-9a-fA-F]{16}', parts[0])
                or not re.fullmatch(r'[A-Za-z?]', parts[1]) or not re.fullmatch(r'[A-Za-z0-9_.$]+', parts[2])):
            complete = False
            continue
        address = int(parts[0], 16)
        if not address:
            continue
        name, kind = parts[2], parts[1]
        if name in {'_text','_stext','_etext'}:
            named[name] = hex(address)
        if kind in 'tTwW':
            entries.append({'name': name, 'address': hex(address), 'type': kind})
    if not entries or not named.get('_text') or not named.get('_etext'):
        raise ValueError('symbol_addresses_restricted_or_missing')
    if int(named['_etext'], 16) <= int(named['_text'], 16):
        raise ValueError('symbol_table_invalid')
    entries.sort(key=lambda entry: int(entry['address'], 16))
    return entries, named, complete


def _write(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(value if isinstance(value, bytes) else (json.dumps(value, indent=2) + '\n').encode())
    os.replace(temporary, path)


def capture(acceptance, directory, case, expected_boot_id):
    """Read independent capabilities before a lease; missing notes is not lost SSH."""
    directory = Path(directory)
    started = time.monotonic()
    deadline = started + 25
    cases = {'planned-reboot', 'restore-mac', 'ordinary-reboot'}
    valid_boot = isinstance(expected_boot_id, str) and bool(UUID.fullmatch(expected_boot_id))
    public = {'case': case if case in cases else 'invalid_case', 'available': False,
              'expected_boot_id': expected_boot_id if valid_boot else None,
              'source': 'authenticated same-boot live /proc/kallsyms; optional GNU notes',
              'fixed_kernel': FIXED_KERNEL, 'reads': {}}
    private = dict(public)
    try:
        marker = directory / '.wrtix-vm'
        if not marker.is_file() or marker.is_symlink():
            return {**public, 'reason': 'witness_fixture_invalid'}
        (directory / 'cpu-witness.private.json').unlink(missing_ok=True)
        if case not in cases or not valid_boot:
            raise ValueError('witness_identity_invalid')
        def read(name, command):
            remaining = min(10, deadline - time.monotonic())
            if remaining <= 0:
                public['reads'][name] = {'available': False, 'reason': 'budget_exhausted', 'attempted': False}
                return None
            try:
                result = acceptance.remote(command, timeout=remaining, check=False)
                public['reads'][name] = {'available': result.returncode == 0, 'attempted': True,
                                         'returncode': result.returncode}
                if result.returncode:
                    public['reads'][name]['reason'] = 'SSH_or_remote_255' if result.returncode == 255 else 'command_nonzero'
                    return None
                public['reads'][name]['bytes'] = len(result.stdout)
                return result.stdout
            except Exception:
                public['reads'][name] = {'available': False, 'reason': 'read_exception_or_timeout', 'attempted': True}
                return None
        def metadata(name):
            raw = read(name, META)
            if raw is not None:
                try:
                    value = parse_meta(raw)
                    public[name] = value
                    return value
                except (ValueError, UnicodeError):
                    public['reads'][name]['parse'] = 'invalid'
            return None
        before = metadata('boot_before')
        notes = read('notes', 'cat /sys/kernel/notes')
        symbols = read('symbols', 'cat /proc/kallsyms')
        after = metadata('boot_after')
        build_id = None
        if notes is not None:
            public['kernel_notes_sha256'] = hashlib.sha256(notes).hexdigest()
            try:
                build_id = parse_notes(notes)
            except ValueError:
                public['reads']['notes']['parse'] = 'GNU_build_id_unavailable'
        public['kernel'] = {'release': before['release'] if before else None, 'build_id': build_id}
        public['fixed_ELF_binding'] = bool(before and build_id == FIXED_KERNEL['build_id']
                                          and before['release'] == FIXED_KERNEL['release'])
        if build_id is not None:
            public['build_id_matches_fixed'] = build_id == FIXED_KERNEL['build_id']
        if before is None or after is None:
            raise ValueError('boot_metadata_unavailable')
        if before['boot_id'] != expected_boot_id or after['boot_id'] != expected_boot_id:
            raise ValueError('witness_boot_changed')
        if symbols is None:
            raise ValueError('symbol_table_unavailable')
        public['symbol_table_sha256'] = hashlib.sha256(symbols).hexdigest()
        entries, named, complete = parse_symbols(symbols)
        public.update({'available': True, 'symbols_complete': complete,
                       'symbol_basis': 'authenticated same-boot live /proc/kallsyms',
                       'symbol_count': len(entries),
                       'kernel_text': {'start': named['_text'], 'end': named['_etext']}})
        if public['fixed_ELF_binding']:
            public['kaslr_slide'] = hex(int(named['_text'], 16) - int(FIXED_KERNEL['text_link_address'], 16))
        private = {**public, 'boot_id': expected_boot_id, 'symbols': named, 'symbol_entries': entries}
        _write(directory / (case + '.kallsyms.private'), symbols)
    except Exception as error:
        known = {'witness_identity_invalid', 'boot_metadata_unavailable', 'witness_boot_changed',
                 'symbol_table_unavailable', 'symbol_addresses_restricted_or_missing', 'symbol_table_invalid'}
        public['available'] = False
        public['reason'] = str(error) if str(error) in known else 'witness_read_or_parse_unavailable'
        private = dict(public)
    public['elapsed_seconds'] = round(time.monotonic() - started, 3)
    private['elapsed_seconds'] = public['elapsed_seconds']
    try:
        _write(directory / 'cpu-witness.private.json', private)
        destination = acceptance.directory / 'reboot-witnesses.json'
        existing = json.loads(destination.read_text()) if destination.exists() else {}
        if not isinstance(existing, dict):
            raise ValueError('invalid_witness_record')
        existing[case] = public
        _write(destination, existing)
    except (OSError, ValueError, TypeError):
        public['available'] = False
        public['reason'] = 'witness_storage_unavailable'
        try:
            (directory / 'cpu-witness.private.json').unlink(missing_ok=True)
        except OSError:
            pass
    return public
