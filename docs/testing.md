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

Release CI additionally builds with the pinned official SDK and runs
`tests/vm/package_acceptance.py` against that exact signed artifact. It verifies
installation without bypassing signature checks, idle replacement/removal,
pending-transaction package fences, and the controller acceptance above. Raw VM
credentials and receipts are not published. This suite requires networking and
virtualization and remains outside `nix flake check`.

The architecture-independent local helper APK is not an SDK build. Full image
compilation, sysupgrade across firmware versions, physical radio performance,
VLAN/PPE behavior and consumer-specific roaming tests remain separate downstream
responsibilities. Do not infer them from a successful VM test. A consumer profile
should run its semantic and runtime checks through the public extension API and
retain any hardware acceptance results privately.

The workflow regressions cover concise UCI conversion, typed provider references,
plain capture and selected-field encryption, anonymous section ordering, list references, provider failure
and timeout handling, package prerequisites and preparation fences. These run
under `nix flake check`. Target fixtures exercise package preparation failure,
retry, idempotency and rejection during a pending configuration transaction.

Workflow acceptance on official OpenWrt 25.12.0 x86/64 exercised real SSH capture
of nine UCI packages, SOPS encryption, generated Nix builds, initial deployment
and repeat deployment returning noop. Parsed UCI values and section ordering
matched before and after. Real APK tests exercised missing-package failure,
retry, installing prerequisites, owned commit hooks, concurrent preparation
rejection, foreign hook rejection and blocking preparation during an unconfirmed
configuration transaction. The latter transaction rolled back successfully.
The opkg path is covered with synthetic target fixtures; real opkg acceptance
remains outstanding.
An end-to-end custom provider test supplied a synthetic token only through the
controller's runtime environment. Preview succeeded without a lookup, deployment
produced the expected target file digest, and removing the declaration retired
the owned file. Generated source and command output contained no token value.

Physical ARM64 acceptance also covered plaintext and selectively encrypted
capture, generated Nix builds, concise UCI deployment, provider caching and
failure handling, package preparation retry and noop deployment. Package tests
used a temporary signed local repository. An unconfirmed configuration blocked
package preparation without changing its deadline, then rolled back on timeout.
The original configuration and agent runtime were restored after testing. These
workflow checks do not establish radio performance or hardware-offload behavior.
