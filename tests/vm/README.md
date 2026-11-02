# Official OpenWrt VM acceptance

`image.json` pins the official OpenWrt 25.12.0 x86/64 ext4 combined image and
its SHA-256. `machine.py` verifies that compressed image before preparing a
private copy. The upstream checksum is published alongside the image:
<https://downloads.openwrt.org/releases/25.12.0/targets/x86/64/sha256sums>.

The harness requires Python 3, QEMU x86_64, `debugfs`, OpenSSH and network
access. KVM is preferred; QEMU falls back to emulation when unavailable.
Keep the working directory outside the Git checkout. It contains a disposable
private SSH key, guest disk, console logs and transaction evidence.

```sh
python3 tests/vm/machine.py --directory /tmp/wrtix-vm prepare
python3 tests/vm/machine.py --directory /tmp/wrtix-vm start
python3 tests/vm/machine.py --directory /tmp/wrtix-vm ssh 'cat /etc/openwrt_release'
```

The harness injects only test connectivity and an ephemeral authorized key
before boot. It does not bridge the guest into a physical LAN. QEMU user-mode
networking exposes SSH on loopback: `127.0.0.1:22080` reaches `10.0.2.15`,
while `127.0.0.2:22080` and `127.0.0.1:22081` reach `10.0.2.16`. These two
addresses permit an actual management-address handoff without changing the
controller's SSH port. Guest Internet access is available for official packages.

To exercise the feed install recipe without compiling a complete firmware,
use apk-tools **3** to build a local test artifact:

```sh
python3 tests/vm/build-package.py --apk /path/to/apk --output /tmp/wrtix-agent.apk
```

The helper evaluates the feed's install recipe and scriptlets, including the
OpenWrt default post-install/pre-remove wrappers. This artifact is unsigned;
use `--allow-untrusted` only in the disposable VM. A matching OpenWrt SDK/build
tree remains the release package builder. This shortcut does not prove an SDK
build or firmware image has succeeded.

Install `jq`, `flock` and `coreutils-timeout` from the official guest repositories,
copy the APK over SSH and install it. Installation must leave network and shadow
file hashes unchanged. Test real package upgrade/removal both when idle and
while a configuration transaction is pending. The latter must fail before any
runtime or guard is removed. After controller acceptance, stop the guest:

```sh
python3 tests/vm/machine.py --directory /tmp/wrtix-vm stop
```

The controller acceptance suite should cover registration, idempotency, actual
address handoff, controller interruption, explicit confirmation, reboot,
unchanged transaction deadlines, timeout rollback, receipt-based recovery and
required-check failure. Configuration rollback must restore authentication and
unregistered identity for a failed first takeover. Report each result separately;
a successful boot or simulated target test does not establish these properties.

No VM test substitutes for downstream hardware, radio, VLAN or acceleration
acceptance. Those tests belong to the consumer project.

Run the controller's real transaction suite after installing the package:

```sh
python3 tests/vm/controller_acceptance.py \
  --key /tmp/wrtix-vm/identity \
  --known-hosts /tmp/wrtix-vm/known_hosts \
  --output /tmp/wrtix-vm-acceptance
```

It requires an unenrolled VM, deliberately changes its configuration, and restores
the original management address and MAC. It leaves a registered synthetic device
for further inspection. Results include individual passes and explicit exclusions.
