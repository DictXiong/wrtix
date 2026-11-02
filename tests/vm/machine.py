#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Isolated, disposable official OpenWrt VM; never attaches host interfaces."""
from __future__ import annotations
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import struct
import subprocess
import time

PIN = json.loads(Path(__file__).with_name("image.json").read_text())


def run(argv, **kwargs):
    return subprocess.run(argv, check=True, **kwargs)


def prepare(directory, image):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise RuntimeError("VM directory must be empty")
    if hashlib.sha256(image.read_bytes()).hexdigest() != PIN["sha256"]:
        raise RuntimeError("official image checksum mismatch")
    (directory / ".wrtix-vm").touch(mode=0o600)
    disk = directory / "disk.img"
    with gzip.open(image, "rb") as src, disk.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    key = directory / "identity"
    run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)])
    with disk.open("rb") as f:
        mbr = f.read(512)
        offset, sectors = struct.unpack_from("<II", mbr, 446 + 16 + 8)
        if mbr[446 + 16 + 4] != 0x83 or offset == 0 or sectors == 0:
            raise RuntimeError("unexpected official root partition layout")
        f.seek(offset * 512)
        root = directory / "root.ext4"
        with root.open("wb") as dst:
            dst.write(f.read(sectors * 512))
    setup = directory / "setup.sh"
    setup.write_text("""#!/bin/sh
uci -q delete network.@device[0]
uci set network.lan.device='eth0'
uci set network.lan.proto='static'
uci set network.lan.ipaddr='10.0.2.15'
uci set network.lan.netmask='255.255.255.0'
uci set network.lan.gateway='10.0.2.2'
uci set network.lan.dns='10.0.2.3'
uci set network.lan.ip6assign=''
uci commit network
exit 0
""")
    commands = directory / "debugfs.commands"
    commands.write_text(
        "mkdir /etc/dropbear\n"
        f"write {key}.pub /etc/dropbear/authorized_keys\n"
        "set_inode_field /etc/dropbear/authorized_keys mode 0100600\n"
        f"write {setup} /etc/uci-defaults/99-wrtix-vm\n"
        "set_inode_field /etc/uci-defaults/99-wrtix-vm mode 0100700\n"
    )
    with (directory / "image-prepare.log").open("w") as log:
        run(["debugfs", "-w", "-f", str(commands), str(root)], stdout=log, stderr=log)
    with disk.open("r+b") as dst, root.open("rb") as src:
        dst.seek(offset * 512)
        shutil.copyfileobj(src, dst)
    root.unlink()


def checked(directory):
    if not (directory / ".wrtix-vm").is_file():
        raise RuntimeError("not a disposable wrtix VM directory")


def start(directory, port, alternate_port):
    checked(directory)
    if (directory / "machine.json").exists():
        raise RuntimeError("VM already started; stop it first")
    monitor = directory / "monitor.sock"
    monitor.unlink(missing_ok=True)
    accel = "kvm" if os.access("/dev/kvm", os.R_OK | os.W_OK) else "tcg"
    args = ["qemu-system-x86_64", "-accel", accel, "-m", "384", "-smp", "2",
            "-drive", f"file={directory / 'disk.img'},format=raw,if=virtio",
            "-device", "virtio-net-pci,netdev=management",
            "-netdev", f"user,id=management,hostfwd=tcp:127.0.0.1:{port}-10.0.2.15:22,hostfwd=tcp:127.0.0.1:{alternate_port}-10.0.2.16:22,hostfwd=tcp:127.0.0.2:{port}-10.0.2.16:22",
            "-display", "none", "-chardev", f"socket,id=serial,path={directory / 'serial.sock'},server=on,wait=off,logfile={directory / 'console.log'}", "-serial", "chardev:serial",
            "-monitor", f"unix:{monitor},server=on,wait=off", "-no-reboot"]
    # Reboots are required by the acceptance suite; QEMU must survive them.
    args.remove("-no-reboot")
    with (directory / "qemu.log").open("ab") as log:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=log, start_new_session=True)
    time.sleep(0.2)
    if process.poll() is not None:
        raise RuntimeError("QEMU exited; see qemu.log")
    ticks = Path(f"/proc/{process.pid}/stat").read_text().split()[21]
    info = {"pid": process.pid, "start_ticks": ticks, "port": port,
            "alternate_port": alternate_port, "accel": accel, "image_sha256": PIN["sha256"]}
    (directory / "machine.json").write_text(json.dumps(info))
    print(json.dumps(info))


def stop(directory):
    checked(directory)
    info = json.loads((directory / "machine.json").read_text())
    stat = Path(f"/proc/{info['pid']}/stat")
    if stat.exists() and stat.read_text().split()[21] == info["start_ticks"]:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(str(directory / "monitor.sock"))
            connection.sendall(b"quit\n")
        for _ in range(50):
            if not stat.exists() or stat.read_text().split()[2] == "Z":
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("VM did not exit")
    (directory / "machine.json").unlink()


def ssh_args(directory, alternate=False):
    checked(directory)
    info = json.loads((directory / "machine.json").read_text())
    return ["ssh", "-i", str(directory / "identity"), "-p", str(info["alternate_port" if alternate else "port"]),
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", "-o", "IdentitiesOnly=yes",
            "-o", f"UserKnownHostsFile={directory / 'known_hosts'}", "-o", "StrictHostKeyChecking=accept-new",
            "root@127.0.0.1"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("prepare")
    init.add_argument("--image", required=True, type=Path)
    boot = sub.add_parser("start")
    boot.add_argument("--port", type=int, default=22080)
    boot.add_argument("--alternate-port", type=int, default=22081)
    sub.add_parser("stop")
    ssh = sub.add_parser("ssh")
    ssh.add_argument("--alternate", action="store_true")
    ssh.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    directory = args.directory.resolve()
    # debugfs command paths are not shell-interpreted, but whitespace is still
    # syntax to debugfs. Keep the fixture's generated command files unambiguous.
    if any(c.isspace() for c in str(directory)):
        parser.error("VM directory must not contain whitespace")
    if args.command == "prepare": prepare(directory, args.image.resolve())
    elif args.command == "start": start(directory, args.port, args.alternate_port)
    elif args.command == "stop": stop(directory)
    else: raise SystemExit(subprocess.call(ssh_args(directory, args.alternate) + args.args))


if __name__ == "__main__":
    main()
