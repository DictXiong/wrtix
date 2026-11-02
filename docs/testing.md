# Testing and limits

Run `nix flake check` for the host system. It evaluates public NixOS and
standalone interfaces, synthetic profile/fleet extensions, ownership conflicts
and disabled behavior; builds the tools; and runs controller, simulated target,
controller-to-agent and sysupgrade inventory regressions. The CI uses this same
entry point. No device credentials or live inventory are required.

The initial x86_64 acceptance ran 34 controller, 43 inherited SSH/SOPS/receipt,
55 target, 5 controller-to-agent and 7 package-lifecycle/backup cases. These are regression
counts, not independent proofs of all platform behavior. aarch64 is exported and
evaluated but was not executed on an aarch64 build host.

The [official OpenWrt VM suite](../tests/vm/README.md) is separate because it needs
network access and virtualization. Initial acceptance used pinned OpenWrt 25.12.0
x86_64 and an APK produced from the actual feed recipe. It verified:

- Package installation and upgrade preserve the existing network and password.
- Pending transactions prevent APK removal before runtime files are deleted.
- First enrollment, failed enrollment restoring unregistered state, and noop.
- Timeout and explicit rollback, password restoration and receipt recovery.
- Required-check failure, a killed controller and sequential transactions.
- An actual management address transition with manual endpoint entry.
- MAC-triggered reboot, authenticated resume and restoration by another reboot.

The feed also rejects unrelated operation fences instead of overwriting them.
APK pre-removal scripts alone cannot enforce this: the pre-commit hook is part of
the protection. The VM uses APK 3; older opkg-based releases have not received the
same real package-manager acceptance.

The architecture-independent local test APK is not an SDK build. Full image
compilation, sysupgrade across firmware versions, physical radio performance,
VLAN/PPE behavior and consumer-specific roaming tests remain separate downstream
responsibilities. Do not infer them from a successful VM test. A consumer profile
should run its semantic and runtime checks through the public extension API and
retain any hardware acceptance results privately.
