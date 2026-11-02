#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Destructive integration acceptance for an isolated official OpenWrt VM.

Requires a freshly installed wrtix-agent and two SSH forwards, at the same
port, to guest addresses 10.0.2.15 and 10.0.2.16. Never point this at a router.
All receipts, credentials supplied by the caller, and logs remain outside Git.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
import pty
import select
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src/wrtix'))
import compiler


class Acceptance:
    def __init__(self, args):
        self.args = args
        self.directory = args.output.resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.env = dict(os.environ, NIX_SSHOPTS='-i ' + shlex.quote(str(args.key.resolve())))
        self.host = args.host
        self.ssh = ['ssh', '-i', str(args.key.resolve()), '-p', str(args.port), '-o', 'BatchMode=yes',
                    '-o', 'ConnectTimeout=3', '-o', 'UserKnownHostsFile=' + str(args.known_hosts.resolve()),
                    '-o', 'StrictHostKeyChecking=yes']
        self.results = []

    def remote(self, command, *, host=None, data=None, check=True):
        result = subprocess.run([*self.ssh, 'root@' + (host or self.host), command], input=data,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40)
        if check and result.returncode:
            raise RuntimeError('VM command failed (details retained privately)')
        return result

    def save(self, name, data):
        path = self.directory / name
        path.write_bytes(data if isinstance(data, bytes) else json.dumps(data, indent=2).encode())
        path.chmod(0o600)
        return path

    def node(self, **extra):
        return {'target_host': self.host, 'ssh_port': self.args.port, **extra}

    def command(self, action, node=None, *, name, options=(), expected=0, asynchronous=False, address_input=None):
        argv = [sys.executable, str(ROOT / 'src/wrtix/openwrt.py')]
        if node is not None:
            manifest = {'schema_version': 1, 'kind': 'openwrt-fleet', 'devices': {'vm': node}}
            path = self.save(name + '.manifest.json', manifest)
            argv += ['--manifest', str(path)]
        argv += [action, '--device', 'vm', '--known-hosts', str(self.args.known_hosts.resolve()),
                 '--port', str(self.args.port), *options]
        if address_input is not None:
            pid, descriptor = pty.fork()
            if pid == 0:
                os.execve(sys.executable, argv, self.env)
            transcript = bytearray()
            sent = False
            deadline = time.monotonic() + 300
            try:
                while time.monotonic() < deadline:
                    if select.select([descriptor], [], [], 1)[0]:
                        try:
                            chunk = os.read(descriptor, 65536)
                        except OSError:
                            break
                        if not chunk:
                            break
                        transcript.extend(chunk)
                        if b"Post-switch SSH address (" in transcript and not sent:
                            os.write(descriptor, address_input.encode() + b"\n")
                            sent = True
                    child, status = os.waitpid(pid, os.WNOHANG)
                    if child:
                        break
                else:
                    os.kill(pid, signal.SIGKILL)
                    raise RuntimeError("interactive address prompt timed out")
            finally:
                os.close(descriptor)
            try:
                _, status = os.waitpid(pid, 0)
            except ChildProcessError:
                pass
            self.save(name + '.terminal', bytes(transcript))
            if os.waitstatus_to_exitcode(status) != expected or not sent:
                raise RuntimeError(name + ' interactive handoff failed; inspect private transcript')
            return None
        if asynchronous:
            stdout = (self.directory / (name + '.stdout')).open('wb')
            stderr = (self.directory / (name + '.stderr')).open('wb')
            process = subprocess.Popen(argv, env=self.env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                       start_new_session=True)
            stdout.close(); stderr.close()
            return process
        result = subprocess.run(argv, env=self.env, stdin=subprocess.DEVNULL, capture_output=True, timeout=330)
        self.save(name + '.stdout', result.stdout)
        self.save(name + '.stderr', result.stderr)
        if result.returncode != expected:
            raise RuntimeError(name + ' returned ' + str(result.returncode) + '; inspect private stderr')
        return json.loads(result.stdout) if result.stdout.strip() and expected == 0 else result

    def receipt(self, name):
        return str(self.directory / (name + '.receipt.json'))

    def passed(self, name):
        self.results.append(name)
        self.save('results.json', {'passed': self.results})
        print('PASS ' + name, flush=True)

    def status(self):
        return json.loads(self.remote('/usr/libexec/wrtix/agent status').stdout)

    def wait_state(self, state, timeout=100):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.remote('/usr/libexec/wrtix/agent status', check=False)
            if result.returncode == 0 and json.loads(result.stdout).get('state') == state:
                return json.loads(result.stdout)
            time.sleep(2)
        raise RuntimeError('VM did not reach state ' + state)

    def run(self):
        identity = json.loads(self.remote('/usr/libexec/wrtix/agent identity').stdout)
        if identity['identity']['device_id']:
            raise RuntimeError('acceptance requires a freshly unenrolled isolated VM')
        release = self.remote('. /etc/openwrt_release; printf "%s" "$DISTRIB_RELEASE"').stdout
        if not release.startswith(b'25.12.'):
            raise RuntimeError('official OpenWrt 25.12 VM required')
        before = self.remote('sha256sum /etc/shadow /etc/config/network /etc/dropbear/authorized_keys').stdout
        original_network = self.remote('cat /etc/config/network').stdout
        self.save('original-network', original_network)
        first = self.node(files={'/etc/wrtix-first-probe': {'text': 'candidate', 'mode': '0600'}})
        self.command('deploy', first, name='first-unconfirmed', options=(
            '--initialize', '--target-host', self.host, '--ttl', '60',
            '--receipt', self.receipt('first-unconfirmed')))
        self.wait_state('rolled-back', 90)
        assert json.loads(self.remote('/usr/libexec/wrtix/agent identity').stdout)['identity']['device_id'] == ''
        assert self.remote('test ! -e /etc/wrtix-first-probe', check=False).returncode == 0
        assert self.remote('sha256sum /etc/shadow /etc/config/network /etc/dropbear/authorized_keys').stdout == before
        self.passed('failed first takeover restores unregistered identity and original network/authentication')
        minimal = self.node()
        result = self.command('deploy', minimal, name='initialize', options=(
            '--initialize', '--target-host', self.host, '--receipt', self.receipt('initialize'), '--auto-confirm', '--ttl', '180'))
        assert result['state'] == 'confirmed'
        assert self.remote('sha256sum /etc/shadow /etc/config/network /etc/dropbear/authorized_keys').stdout == before
        self.passed('initial enrollment preserves existing network, password and keys')
        assert self.command('deploy', minimal, name='noop')['state'] == 'noop'
        self.passed('identical deployment is noop')

        changed = self.node(files={'/etc/wrtix-vm-probe': {'text': 'candidate\n', 'mode': '0600'}})
        self.command('deploy', changed, name='timeout', options=('--receipt', self.receipt('timeout'), '--ttl', '60'))
        assert self.remote('cat /etc/wrtix-vm-probe').stdout == b'candidate\n'
        self.wait_state('rolled-back', 90)
        assert self.remote('test ! -e /etc/wrtix-vm-probe', check=False).returncode == 0
        self.command('status', name='timeout-receipt', options=('--receipt', self.receipt('timeout')))
        self.passed('unconfirmed deployment expires and receipt recovers rolled-back state')

        self.command('deploy', changed, name='recover', options=('--receipt', self.receipt('recover'), '--ttl', '180'))
        assert self.command('confirm', name='recover-confirm', options=('--receipt', self.receipt('recover')))['state'] == 'confirmed'
        self.passed('receipt confirms without a manifest or original controller process')

        reject = self.node(files={'/usr/libexec/wrtix-managed/reject': {'text': '#!/bin/sh\ncat >/dev/null\nexit 1\n', 'mode': '0700'}},
            checks={'reject': {'phase': 'preconfirm', 'executable': '/usr/libexec/wrtix-managed/reject',
                               'input': {'secret': 'synthetic-private-value'}}})
        self.command('deploy', reject, name='required-check', expected=1, options=(
            '--receipt', self.receipt('required-check'), '--auto-confirm', '--ttl', '60'))
        assert self.status()['state'] != 'confirmed'
        self.wait_state('rolled-back', 90)
        assert self.remote('cat /etc/wrtix-vm-probe').stdout == b'candidate\n'
        self.passed('required preconfirm failure cannot cancel deadline or bypass rollback')

        shadow_before = self.remote('sha256sum /etc/shadow').stdout
        temporary_auth = self.node(users={'root': {'passwordHash': '$6$synthetic$' + 'A' * 86}})
        self.command('deploy', temporary_auth, name='auth-rollback', options=(
            '--receipt', self.receipt('auth-rollback'), '--ttl', '120'))
        assert self.remote('sha256sum /etc/shadow').stdout != shadow_before
        self.command('rollback', name='auth-recovery', options=('--receipt', self.receipt('auth-rollback')))
        self.wait_state('rolled-back', 60)
        assert self.remote('sha256sum /etc/shadow').stdout == shadow_before
        self.passed('password change rollback restores original authentication')

        # Start an apply whose service restart remains active, then kill only the
        # controller process. The packaged target guard owns recovery throughout.
        slow = self.node(files={'/etc/init.d/wrtix-managed-vm-probe': {'text': '#!/bin/sh /etc/rc.common\nSTART=99\nstart() { sleep 12; }\nstop() { :; }\n', 'mode': '0755'}},
                         services={'wrtix-managed-vm-probe': {'enable': True, 'action': 'restart'}})
        process = self.command('deploy', slow, name='interrupted', asynchronous=True,
                               options=('--receipt', self.receipt('interrupted'), '--ttl', '180'))
        try:
            self.wait_state('applied', 60)
            process.kill(); process.wait(timeout=10)
        finally:
            if process.poll() is None:
                process.kill(); process.wait()
        self.command('rollback', name='interrupted-recover', options=('--receipt', self.receipt('interrupted')))
        self.wait_state('rolled-back', 90)
        self.passed('killed controller recovers via saved receipt and target guard')

        network = compiler.parse_uci(original_network)
        network['lan']['options']['ipaddr'] = '10.0.2.16'
        moved = self.node(uci={'network': network}, services={'network': {'enable': True, 'action': 'reload', 'reloadOn': {'uci': ['network'], 'files': []}}})
        # Fixed alternate endpoint uses the same host key; it is NOT trusted by
        # scanning an unauthenticated address after the network change.
        self.command('deploy', moved, name='handoff', options=('--receipt', self.receipt('handoff'), '--ttl', '180',
                                                              '--ask-post-switch-host'), address_input=self.args.alternate_host)
        assert self.command('confirm', name='handoff-confirm', options=('--receipt', self.receipt('handoff')))['state'] == 'confirmed'
        self.host = self.args.alternate_host
        # Standalone direct probes pin the old host key exactly as the controller.
        self.ssh += ['-o', 'HostKeyAlias=[' + self.args.host + ']:' + str(self.args.port)]
        assert self.remote('uci -q get network.lan.ipaddr').stdout.strip() == b'10.0.2.16'
        self.passed('real management address transition, manual prompt and independent receipt confirmation')

        restore = self.node(uci={'network': compiler.parse_uci(original_network)},
                            services={'network': {'enable': True, 'action': 'reload', 'reloadOn': {'uci': ['network'], 'files': []}}})
        # Supply the known original host key alias for the connection from .16.
        self.env['NIX_SSHOPTS'] += ' -o ' + shlex.quote('HostKeyAlias=[' + self.args.host + ']:' + str(self.args.port))
        self.command('deploy', restore, name='restore-address', options=(
            '--receipt', self.receipt('restore-address'), '--ttl', '180', '--post-switch-host', self.args.host, '--auto-confirm'))
        self.host = self.args.host
        self.passed('management address restored through a guarded transaction')
        # The generic agent detects MAC changes from UCI and requires one
        # authenticated reboot before confirmation, preserving the original TTL.
        changed_mac = copy.deepcopy(restore)
        changed_mac['target_host'] = self.host
        changed_mac['uci']['network']['lan']['options']['macaddr'] = '02:00:00:00:00:27'
        before_boot = json.loads(self.remote('/usr/libexec/wrtix/agent identity').stdout)['boot_id']
        self.command('deploy', changed_mac, name='planned-reboot', options=(
            '--receipt', self.receipt('planned-reboot'), '--ttl', '300', '--auto-confirm'))
        after = json.loads(self.remote('/usr/libexec/wrtix/agent identity').stdout)
        assert after['boot_id'] != before_boot
        assert self.remote('cat /sys/class/net/eth0/address').stdout.strip() == b'02:00:00:00:00:27'
        self.passed('MAC change requires authenticated reboot and resumes original transaction')
        restore['target_host'] = self.host
        self.command('deploy', restore, name='restore-mac', options=(
            '--receipt', self.receipt('restore-mac'), '--ttl', '300', '--auto-confirm'))
        self.passed('original network and MAC restored through second guarded reboot')
        self.save('summary.json', {'openwrt': release.decode(), 'passed': self.results,
                                  'not_covered': ['wireless and hardware acceleration (VM has neither)',
                                                  'physical device/AP-specific checks']})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key', type=Path, required=True)
    parser.add_argument('--known-hosts', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--alternate-host', default='127.0.0.2')
    parser.add_argument('--port', type=int, default=22080)
    args = parser.parse_args()
    Acceptance(args).run()


if __name__ == '__main__':
    main()
