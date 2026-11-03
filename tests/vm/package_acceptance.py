#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Accept the actual signed SDK artifact in an isolated official OpenWrt VM."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import time

import machine
from controller_acceptance import Acceptance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', required=True, type=Path)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--image', type=Path)
    args = parser.parse_args()
    artifacts, work = args.artifacts.resolve(), args.directory.resolve()
    if work.exists():
        parser.error('directory must not exist; this test creates a disposable VM')
    work.mkdir(mode=0o700, parents=True)
    metadata = json.loads((artifacts / 'release.json').read_text())
    package = artifacts / metadata['package']
    public = artifacts / metadata['signing_key']
    if not package.is_file() or not public.is_file():
        raise RuntimeError('package and signing key must exist')
    if hashlib.sha256(package.read_bytes()).hexdigest() != metadata['package_sha256']:
        raise RuntimeError('release package checksum mismatch')
    image = args.image.resolve() if args.image else work / 'openwrt.img.gz'
    if not args.image:
        subprocess.run(['curl', '--fail', '--location', '--retry', '3', '--output', str(image),
                        machine.PIN['url']], check=True)
    vm = work / 'guest'
    machine.prepare(vm, image)
    machine.start(vm, 22080, 22081)
    passed = []

    def remote(command, data=None, check=True):
        result = subprocess.run(machine.ssh_args(vm) + [command], input=data, capture_output=True, timeout=180)
        if check and result.returncode:
            (work / 'last-command.stderr').write_bytes(result.stderr)
            raise RuntimeError('VM package command failed: ' + command)
        return result

    def success(name):
        passed.append(name)
        print('PASS ' + name, flush=True)

    def hashes():
        return remote('sha256sum /etc/config/network /etc/shadow /etc/dropbear/authorized_keys').stdout

    try:
        deadline = time.monotonic() + 180
        while remote('true', check=False).returncode:
            if time.monotonic() >= deadline:
                raise RuntimeError('official VM did not become reachable')
            time.sleep(2)
        remote('apk update && apk add jq flock coreutils-timeout')
        before = hashes()
        remote('mkdir -p /tmp/wrtix-release')
        package_path = '/tmp/wrtix-release/' + shlex.quote(package.name)
        remote('cat > ' + package_path, package.read_bytes())
        remote('cat > /tmp/wrtix-release/packages.adb', (artifacts / 'packages.adb').read_bytes())
        install = 'apk add ' + package_path
        replace = 'apk --no-network --repository /tmp/wrtix-release/packages.adb fix --reinstall wrtix-agent'
        remote('cat > /etc/apk/keys/' + shlex.quote(public.name), public.read_bytes())
        # No allow-untrusted: installation must authenticate the actual SDK APK.
        remote(install)
        installed = remote('apk info -e ' + shlex.quote('wrtix-agent=' + metadata['package_version']), check=False)
        if installed.returncode:
            raise RuntimeError('installed version differs from the release metadata')
        if hashes() != before:
            raise RuntimeError('package installation changed network or authentication')
        success('signed SDK package installs with dependencies and preserves network/authentication')
        remote(replace)
        if hashes() != before:
            raise RuntimeError('package reinstall changed network or authentication')
        success('idle package replacement preserves network/authentication')

        acceptance = Acceptance(argparse.Namespace(
            key=vm / 'identity', known_hosts=vm / 'known_hosts', output=work / 'controller',
            host='127.0.0.1', alternate_host='127.0.0.2', port=22080))
        acceptance.run()
        success('controller enrollment, handoff, receipts, guarded reboot and rollback acceptance')
        pending = acceptance.node(files={'/etc/wrtix-package-probe': {'text': 'candidate\n', 'mode': '0600'}})
        receipt = acceptance.receipt('package-fence')
        acceptance.command('deploy', pending, name='package-fence', options=('--receipt', receipt, '--ttl', '180'))
        immutable = remote('sha256sum /usr/libexec/wrtix/agent /etc/init.d/wrtix-agent /usr/share/wrtix/contract.json').stdout
        for operation in ('apk del wrtix-agent', replace):
            if remote(operation, check=False).returncode == 0:
                raise RuntimeError('pending transaction did not veto package operation')
            if remote('sha256sum /usr/libexec/wrtix/agent /etc/init.d/wrtix-agent /usr/share/wrtix/contract.json').stdout != immutable:
                raise RuntimeError('rejected package operation changed runtime')
        success('pending transaction rejects removal and replacement before runtime mutation')
        acceptance.command('rollback', name='package-fence-restore', options=('--receipt', receipt))
        acceptance.wait_state('rolled-back', 90)
        before = hashes()
        enrolled = json.loads(remote('/usr/libexec/wrtix/agent identity').stdout)['identity']
        ledger = remote('sha256sum /etc/wrtix/managed.json').stdout
        remote('apk del wrtix-agent')
        if remote('test ! -e /usr/libexec/wrtix/agent', check=False).returncode != 0 or hashes() != before:
            raise RuntimeError('idle package removal did not preserve user configuration')
        remote(install)
        if json.loads(remote('/usr/libexec/wrtix/agent identity').stdout)['identity'] != enrolled:
            raise RuntimeError('idle removal/reinstall lost enrolled identity')
        if remote('sha256sum /etc/wrtix/managed.json').stdout != ledger:
            raise RuntimeError('idle removal/reinstall changed resource ownership')
        success('idle removal and reinstall retain configuration and enrolled identity')
        summary = {'openwrt': machine.PIN['release'], 'package': package.name,
                   'package_sha256': metadata['package_sha256'], 'passed': passed,
                   'controller_passed': acceptance.results,
                   'not_covered': ['opkg/IPK', 'non-x86 runtime', 'physical radios, VLAN and hardware acceleration',
                                   'firmware sysupgrade']}
        (work / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    finally:
        machine.stop(vm)


if __name__ == '__main__':
    main()
