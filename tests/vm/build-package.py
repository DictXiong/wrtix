#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Exercise the feed install recipe and emit a local acceptance-test APK.

A matching OpenWrt SDK remains the release packaging entry point. This small
harness uses apk-tools 3 and the exact feed install/script definitions, avoiding
a cross compiler for architecture-independent test artifacts.
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
FEED = ROOT / 'feed/wrtix-agent'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apk', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--sign-key', type=Path, help='Optional local signing identity for acceptance packages')
    version = re.sub(r'-(alpha|beta|rc)\.', r'_\1', (ROOT / 'VERSION').read_text().strip()) + '-r1'
    parser.add_argument('--version', default=version)
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='wrtix-package-') as tmp:
        work = Path(tmp)
        (work / 'include').mkdir()
        (work / 'rules.mk').write_text('')
        (work / 'include/package.mk').write_text('')
        package_root = work / 'root'
        harness = work / 'Makefile'
        harness.write_text(f'''TOPDIR := {work}
INCLUDE_DIR := {work}/include
PKG_BUILD_DIR := {work}/build
INSTALL_DIR := install -d
INSTALL_BIN := install -m 0755
INSTALL_DATA := install -m 0644
CP := cp -fpR
include {FEED}/Makefile
.PHONY: fixture
fixture:
\t$(Build/Prepare)
\t$(call Package/wrtix-agent/install,{package_root})
''')
        subprocess.run(['make', '-f', str(harness), 'fixture'], cwd=FEED, check=True,
                       stdout=subprocess.DEVNULL)
        source = (FEED / 'Makefile').read_text()
        scripts = {}
        for name in ('preinst', 'postinst', 'prerm'):
            match = re.search(r'^define Package/wrtix-agent/' + name + r'\n(.*?)^endef$', source, re.M | re.S)
            if not match:
                raise RuntimeError(f'missing feed script {name}')
            path = work / name
            path.write_text(match[1].replace('$$', '$'))
            path.chmod(0o755)
            scripts[name] = path
        # Replicate OpenWrt's standard wrappers, including init service
        # start/stop ordering; this is material to active-transaction safety.
        metadata = package_root / 'lib/apk/packages'
        metadata.mkdir(parents=True)
        listing = metadata / 'wrtix-agent.list'
        listing.write_text(''.join('/' + str(p.relative_to(package_root)) + '\n'
                                   for p in sorted(package_root.rglob('*')) if p.is_file()))
        conffiles = re.search(r'^define Package/wrtix-agent/conffiles\n(.*?)^endef$', source, re.M | re.S)[1]
        (metadata / 'wrtix-agent.conffiles').write_text(conffiles)
        for script, default in [('postinst', 'default_postinst'), ('prerm', 'default_prerm')]:
            body = scripts[script].read_text().split('\n', 1)[1]
            scripts[script].write_text('#!/bin/sh\n. /lib/functions.sh\nexport root="${IPKG_INSTROOT}"\nexport pkgname=wrtix-agent\n' + default + '\n' + body)
        argv = [args.apk, 'mkpkg', '--info', 'name:wrtix-agent', '--info', 'version:' + args.version,
                '--info', 'arch:noarch', '--info', 'license:GPL-2.0-only',
                '--info', 'description:Transactional OpenWrt configuration agent',
                '--info', 'depends:jq uci ubus flock coreutils-timeout coreutils-base64', '--files', str(package_root),
                '--output', str(output)]
        if args.sign_key:
            argv += ['--sign-key', str(args.sign_key.resolve())]
        for event, script in [('pre-install', 'preinst'), ('post-install', 'postinst'),
                              ('pre-upgrade', 'preinst'), ('post-upgrade', 'postinst'),
                              ('pre-deinstall', 'prerm')]:
            argv += ['--script', event + ':' + str(scripts[script])]
        subprocess.run(argv, check=True, env={**os.environ, 'SOURCE_DATE_EPOCH': '0'})
    print(output)


if __name__ == '__main__':
    main()
