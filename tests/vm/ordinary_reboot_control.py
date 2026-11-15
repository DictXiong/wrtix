#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""One agent-free ordinary reboot control in a fresh pinned disposable VM."""
from __future__ import annotations
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import machine
import reboot_witness
from failure_snapshot import capture_on_failure

WAIT_SECONDS = 240
INITIAL_WAIT_SECONDS = 180
FAILURE_SNAPSHOT_SECONDS = 30
TARGET_MAC = '02:00:00:00:00:27'
_UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
_READ = r"""printf 'BOOT '; cat /proc/sys/kernel/random/boot_id
printf 'MAC '; cat /sys/class/net/eth0/address
printf 'NETWORK_SHA '; sha256sum /etc/config/network | cut -d' ' -f1
printf 'ADDRESS '; uci -q get network.lan.ipaddr
configured_mac=$(uci -q get network.lan.macaddr || true); printf 'CONFIGURED_MAC %s\n' "$configured_mac"
if [ -e /usr/libexec/wrtix/agent ] || [ -e /etc/init.d/wrtix-agent ]; then echo 'AGENT present'; else echo 'AGENT absent'; fi
if [ -e /etc/wrtix/transactions/state ]; then echo 'TRANSACTION present'; else echo 'TRANSACTION absent'; fi
"""
_STAGE_MAC = "uci set network.lan.macaddr='02:00:00:00:00:27' && uci commit network"


def _write(path, value):
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2);stream.write('\n')
    os.replace(temporary, path)


def _safe_write(path, value):
    try:
        _write(path, value)
    except (OSError, TypeError, ValueError):
        pass


def parse_identity(data):
    value = {}
    for line in data.decode('ascii', 'strict').splitlines():
        name, _, text = line.partition(' ')
        if name == 'BOOT' and _UUID.fullmatch(text):
            value['boot_id'] = text
        elif name in ('MAC', 'CONFIGURED_MAC') and re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', text):
            value['MAC' if name == 'MAC' else 'configured_MAC'] = text.lower()
        elif name == 'NETWORK_SHA' and re.fullmatch(r'[0-9a-f]{64}', text):
            value['network_sha256'] = text
        elif name == 'ADDRESS' and text == '10.0.2.15':
            value['address'] = text
        elif name in ('AGENT', 'TRANSACTION') and text in ('absent', 'present'):
            value[name.lower()] = text
    if not all(k in value for k in ('boot_id', 'MAC', 'network_sha256', 'address', 'agent', 'transaction')):
        raise ValueError('control_identity_unavailable')
    if value['agent'] != 'absent' or value['transaction'] != 'absent':
        raise ValueError('control_is_not_agent_free')
    return value


class Remote:
    def __init__(self, directory, vm):
        self.directory, self.vm = directory, vm

    def remote(self, command, *, check=True, timeout=8, initial=False):
        # Only the first controlled-fixture connection may establish its key.
        info = json.loads((self.vm / 'machine.json').read_text())
        argv = ['ssh', '-F', '/dev/null', '-i', str(self.vm / 'identity'), '-p', str(info['port']),
                '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
                '-o', 'ConnectionAttempts=1', '-o', 'ConnectTimeout=4',
                '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'UpdateHostKeys=no',
                '-o', 'UserKnownHostsFile=' + str(self.vm / 'known_hosts'),
                '-o', 'StrictHostKeyChecking=' + ('accept-new' if initial else 'yes'),
                'root@127.0.0.1', command]
        result = subprocess.run(argv, capture_output=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError('control_SSH_command_failed')
        return result


def _wait_initial(remote):
    deadline = time.monotonic() + INITIAL_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                break
            result = remote.remote('true', initial=True, check=False, timeout=min(8, remaining))
            if result.returncode == 0:
                known = remote.vm / 'known_hosts'
                if not known.is_file() or known.is_symlink() or not known.stat().st_size:
                    raise RuntimeError('control_initial_hostkey_not_pinned')
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(min(2, max(0, deadline-time.monotonic())))
    raise RuntimeError('control_initial_boot_not_ready')


def _claim_reboot(work, baseline, staged):
    # An existing claim is never removed, overwritten, or used to reboot again.
    record = {'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'case': 'ordinary-reboot', 'reboot_command': '/sbin/reboot', 'force': False,
              'baseline_boot_id': baseline['boot_id'], 'baseline_MAC': baseline['MAC'],
              'target_MAC': TARGET_MAC, 'staged_network_sha256': staged['network_sha256'],
              'wait_seconds': WAIT_SECONDS, 'agent_installed': False, 'transaction_lease': None}
    fd = os.open(work / 'CONTROL-INTENT.json', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(record, stream, indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
    directory_fd = os.open(work, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return record


def _wait_reboot(remote, baseline, deadline):
    last = None
    while time.monotonic() < deadline:
        try:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                break
            result = remote.remote(_READ, check=False, timeout=min(8, remaining))
            if result.returncode == 0:
                last = parse_identity(result.stdout)
                if time.monotonic() >= deadline:
                    break
                if last['boot_id'] != baseline['boot_id'] and last['MAC'] == TARGET_MAC:
                    return last
        except (subprocess.TimeoutExpired, ValueError):
            pass
        time.sleep(min(2, max(0, deadline-time.monotonic())))
    raise RuntimeError('control_reboot_not_verified_within_240_seconds')


def _owned_alive(info):
    if not info:
        return None
    try:
        fields = Path('/proc/' + str(info['pid']) + '/stat').read_text().rpartition(') ')[2].split()
        return fields[19] == str(info['start_ticks']) and fields[0] != 'Z'
    except FileNotFoundError:
        return False
    except (OSError, KeyError, IndexError, TypeError):
        return None


def run(image, work):
    image, work = Path(image).resolve(), Path(work).resolve()
    if any(c.isspace() for c in str(work)):
        raise ValueError('control_directory_whitespace_unsupported')
    # Exclusive fresh directory also prevents a second invocation of this case.
    work.mkdir(mode=0o700, parents=True)
    vm, info, start_attempted = work / 'guest', None, False
    witness = {'available': False, 'case': 'ordinary-reboot'}
    summary = {'case': 'ordinary-reboot', 'outcome': 'INCOMPLETE',
               'image_sha256': machine.PIN['sha256'], 'image_checksum_verified': False, 'accel': 'tcg', 'memory_MiB': 384, 'vCPUs': 2,
               'wait_seconds': WAIT_SECONDS, 'failure_snapshot_seconds': FAILURE_SNAPSHOT_SECONDS,
               'target_MAC': TARGET_MAC, 'agent_installed': False, 'transaction_TTL': None,
               'does_not_override_original_failure': True,
               'differences': ['fresh VM with no wrtix package or transaction',
                               'no guest package installation',
                               'no enrollment or .15-to-.16-to-.15 transaction history; same final .15 network',
                               'same target MAC committed directly with UCI without network reload',
                               'ordinary /sbin/reboot, no -f, invoked once'],
               'stage': 'prepare'}
    try:
        # prepare enforces the exact pinned compressed image checksum.
        machine.prepare(vm, image)
        summary['image_checksum_verified'] = True
        start_attempted = True
        machine.start(vm, 22080, 22081, accel='tcg')
        info = json.loads((vm / 'machine.json').read_text())
        (work / 'controller').mkdir(mode=0o700)
        remote = Remote(work / 'controller', vm)
        summary['stage'] = 'initial_boot'
        _wait_initial(remote)
        summary['baseline'] = baseline = parse_identity(remote.remote(_READ).stdout)
        if baseline['MAC'] == TARGET_MAC:
            raise RuntimeError('control_baseline_MAC_is_already_target')
        summary['stage'] = 'stage_MAC_without_reload'
        remote.remote(_STAGE_MAC)
        summary['staged'] = staged = parse_identity(remote.remote(_READ).stdout)
        if (staged['boot_id'] != baseline['boot_id'] or staged['MAC'] != baseline['MAC']
                or staged.get('configured_MAC') != TARGET_MAC
                or staged['network_sha256'] == baseline['network_sha256']):
            raise RuntimeError('control_MAC_staging_not_verified')
        # A failed/missing witness cannot alter this control's result or lease.
        try:
            public = reboot_witness.capture(remote, vm, 'ordinary-reboot', baseline['boot_id'])
        except Exception:
            public = {'available': False}
        if public.get('available') is True:
            try:
                candidate = json.loads((vm / 'cpu-witness.private.json').read_text())
                if (isinstance(candidate,dict) and candidate.get('available') is True and candidate.get('case') == 'ordinary-reboot'
                        and candidate.get('boot_id') == baseline['boot_id']
                        and candidate.get('symbol_table_sha256') == public.get('symbol_table_sha256')):
                    witness = candidate
            except (OSError, ValueError, TypeError):
                pass
        summary['witness_available'] = witness.get('available') is True
        summary['stage'] = 'ordinary_reboot'
        _claim_reboot(work, baseline, staged)
        summary['reboot_intent_recorded'] = True
        deadline = time.monotonic() + WAIT_SECONDS
        try:
            summary['reboot_command_attempted_once'] = True
            response = remote.remote('/sbin/reboot', check=False, timeout=min(8, deadline-time.monotonic()))
            summary['reboot_command_returncode'] = response.returncode
            if response.returncode not in (0, 255):
                raise RuntimeError('control_reboot_command_failed')
        except subprocess.TimeoutExpired:
            summary['reboot_command_response'] = 'timeout_or_disconnection; command not repeated'
        summary['after'] = _wait_reboot(remote, baseline, deadline)
        summary['stage'], summary['outcome'] = 'verified', 'PASS_CONTROL_ONLY'
    except BaseException:
        summary['outcome'] = 'FAIL_CONTROL_ONLY'
        _safe_write(work / 'summary.json', summary)
        with capture_on_failure(vm, work / 'failure-snapshot.json', FAILURE_SNAPSHOT_SECONDS,
                                cpu_witness=witness):
            raise
    finally:
        primary = sys.exc_info()[0] is not None
        stop_failed = False
        if (vm / 'machine.json').exists():
            try:
                # If startup observation failed after actual launch, still own
                # the machine.json created by this exclusive fixture directory.
                if info is None:
                    info = json.loads((vm / 'machine.json').read_text())
                machine.stop(vm)
            except BaseException:
                stop_failed = True
        alive = _owned_alive(info)
        cleanup = {'start_attempted': start_attempted, 'VM_started': info is not None, 'owned_QEMU_still_alive': alive,
                   'machine_json_absent': not (vm / 'machine.json').exists(),
                   'stop_failed': stop_failed, 'original_failure_preserved': primary}
        cleanup['verified'] = not stop_failed and cleanup['machine_json_absent'] and (alive is False or not start_attempted)
        summary['cleanup'] = cleanup
        if not cleanup['verified']:
            summary['outcome'] = 'FAIL_CONTROL_ONLY'
        if primary:
            _safe_write(work / 'summary.json', summary)
        else:
            _write(work / 'summary.json', summary)
        if not cleanup['verified'] and not primary:
            raise RuntimeError('control_cleanup_not_verified')
    print(json.dumps(summary, sort_keys=True), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, type=Path)
    parser.add_argument('--directory', required=True, type=Path)
    args = parser.parse_args()
    run(args.image, args.directory)


if __name__ == '__main__':
    main()
