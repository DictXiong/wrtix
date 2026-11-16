# Official OpenWrt VM acceptance

`image.json` pins the official OpenWrt 25.12.0 x86/64 ext4 combined image and
its SHA-256. `machine.py` verifies that compressed image before preparing a
private copy. The upstream checksum is published alongside the image:
<https://downloads.openwrt.org/releases/25.12.0/targets/x86/64/sha256sums>.

The fixture extracts `/boot/vmlinuz` read-only from that image's first partition,
checks its pinned size, SHA-256 and Linux boot header, and checks it again before
launch. QEMU loads this exact kernel directly with the official normal GRUB
entry's equivalent root and console arguments, using the same prepared root disk
and no initrd. Guest-initiated reboots remain real reboots; CPU, memory, network,
authentication, boot UUID, MAC, confirmation and rollback checks are unchanged.
This test-only workaround bypasses the GRUB memory-copy stage observed during
a failed emulated reboot. It does not cover the GRUB loading chain or memory layout,
establish a unique GRUB/QEMU cause, or fix product runtime behavior. Earlier
disk-boot failures remain failures.

The harness requires Python 3, QEMU x86_64, `debugfs`, OpenSSH and network
access. KVM is preferred; QEMU falls back to emulation when unavailable.
Use `--accel kvm` or `--accel tcg` to reproduce a specific runner mode; both run
the same transaction checks and deadlines. Keep the working directory outside the Git checkout. It contains a disposable
private SSH key, guest disk, console logs and transaction evidence.

```sh
python3 tests/vm/machine.py --directory /tmp/wrtix-vm prepare --image /path/to/openwrt.img.gz
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

## Release artifact acceptance

To exercise the actual SDK-built and signed package rather than the local test
APK, run from the repository root with Python's PyYAML dependency available:

```sh
python3 ci/build-package.py --work /tmp/wrtix-sdk-build --output /tmp/wrtix-artifacts
python3 tests/vm/package_acceptance.py --artifacts /tmp/wrtix-artifacts \
  --directory /tmp/wrtix-release-vm
```

Both output directories must be new. The SDK builder needs the normal OpenWrt
Linux build prerequisites, curl, zstd and OpenSSL. The VM runner downloads and
checks the pinned image, trusts only the artifact's accompanying public key,
installs without `--allow-untrusted`, exercises the controller and package gates,
and stops its VM on exit. It writes a non-secret `summary.json` alongside private
VM state. CI publishes only the summary and explicitly selected structured diagnostics;
never upload the whole working directory.
The public failure summary retains only known protocol errors and lifecycle fields.
Its status snapshot is taken after the command fails; it is not evidence of the
exact failing predicate. Full controller stderr and receipts remain private.

## Bounded MAC diagnostic

Reuse an existing signed SDK artifact and pinned image, without rebuilding the
SDK or rerunning the complete suite:

```sh
python3 tests/vm/package_acceptance.py --artifacts /path/to/existing-artifacts \
  --image /path/to/pinned-image.img.gz --case mac-reboot --accel tcg \
  --directory /tmp/wrtix-mac-diagnostic
```

The focused path preserves fresh enrollment, both address handoffs and both
protected MAC reboots. It retains the original TTL, wait, identity and actual MAC
checks. Success means only that this one run did not reproduce the failure.

After a failure, the harness spends at most one second on its SSH status probe
and seven seconds on independent QMP/normal-console diagnostics before stopping
the VM. Preinit/failsafe consoles receive no input. The public snapshot contains
only validated fields; missing channels remain unavailable. Original errors and
nonzero exits survive snapshot or cleanup failure. This does not repair the guest
or relax the acceptance criteria.

The normal CI VM job runs the complete suite with `--observe-mac-failures`.
This default-off option extends failure-only observation to at most 30 seconds
only while a current MAC transaction is active and its authenticated preboot
identity is bound to the in-memory witness. Other full-suite failures keep the
seven-second budget without a previous CPU symbol map. Successful MAC cases
clear their observation context before later package lifecycle tests.

Controller wait240, transaction TTL300 and all confirmation/rollback assertions
are unchanged. Observations start only after the original failure and never
late-confirm or turn a recovered guest into a passing case. Diagnostic branch
pushes remain excluded from builds; normal manual, branch, pull-request and tag
runs retain the complete release gates and build their own package.

The failure snapshot separates evidence sources: bounded existing-console boot
milestones, passive live serial, QMP status, owned host QEMU CPU observations,
fixed loopback SSH banners and a strictly authenticated read-only SSH probe.
Milestone classifications describe observed bytes, not a diagnosis of the guest's
actual boot stage. QMP running and increasing host CPU counters do not establish
guest health. Missing or unattempted probes remain unavailable.

Serial input requires positive current-boot normal userspace evidence, excludes
failsafe, and sends one newline before requiring a fresh root prompt. Guest data
is discarded if the boot changes during observation. The structured guest fields
include network/carrier/routes, SSH listening, necessary process and guard state;
raw console lines, complete arguments, credentials and marker contents are never
published. Lock observations remain inode candidates, not proof of ownership or
deadlock. This observer performs no resume, confirmation, repair or reboot.

## x86 reboot localization

The focused MAC diagnostic accepts `--cpu-localization` with the 30-second
failure budget; full acceptance opts in through `--observe-mac-failures`. Before each protected reboot and before its lease
starts, it records the authenticated boot UUID, live symbols and, when available,
the kernel GNU build-id. The fixed-ELF KASLR offset requires that build-id to match. The complete live symbol table stays in the private VM work
directory; only identity/hash/range metadata is included in
`controller/reboot-witnesses.json`. A failed witness never authorizes reuse of an
older map and does not change the original case criteria.

After an already failed case, read-only QMP/HMP obtains two bounded samples of
all vCPUs using their queried indices and thread IDs. Only PC, CS, control/mode
registers, A20, DS/ES address context and halted state are retained. CPU reply indices must agree; samples
are not globally atomic. Physical firmware matches require disabled paging and
the actual CPU-memory FlatView ROM ranges. Kernel matches identify addresses in
the preboot symbol map, not proof of the same startup instance. Unsupported or
unavailable commands remain explicit. No pause/reset, memory dump, repair or
late confirmation is performed; the original failure and cleanup are preserved.

For a specific fixed-image GRUB copy-stub candidate PC, the failed-VM observer can
compare exactly32 bytes at its known RAM address with a normalized opcode
fingerprint. Raw memory is discarded. Only a verified match exposes the three
copy parameters and the necessary count/pointer/direction observations. Binding
the physical code to the executing PC also requires enabled A20. Copy-address
relations require observed flat DS/ES bases; missing or non-flat context stays
explicitly unknown. A stable REP instruction address alone is not proof of a
hang; compare copy progress and keep missing/unsupported observations explicit.
No live memory dump is published.
