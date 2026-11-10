# Architecture

## Configuration and policy

Both `nixosModules.default` and `lib.openwrtConfiguration` use the same
`services.openwrt` module. The former installs the tools only when enabled; the
latter works without evaluating a NixOS system. Neither enables router networking
on the controller. Both expose `config.system.build.openwrt-manifest`,
`openwrt-deploy` and `openwrt-rebuild`.

Devices are extensible Nix submodules. A consumer registers `deviceModules` to
declare typed `profiles.*` options and produce resources. `fleetModules` receives
all enabled base devices and may contribute resources to those devices. This is
a second stage, not a recursive fixed-point: its output is not fed back into the
input fleet. Cross-device address and neighbor policies belong to consumers.

Owner-named `resourceSets` preserve provenance through resource compilation.
UCI, files, services, package prerequisites, optional root authentication,
resolver configuration, identity constraints and checks share one merge and
validation pipeline. Unknown options fail Nix evaluation. Conflicting resources
do not silently depend on import order. Explicit overrides must satisfy ownership
constraints and the final checks.

## Controller

The Nix closure contains public configuration and secret references. During
execution the controller resolves SOPS references using the configured identity,
or invokes registered secret providers through private stdin/stdout,
validates final resources, renders exact UCI bytes and constructs a private
transaction archive. Required compile checks see the final fleet and selected
resources through private stdin. Their output is suppressed.

SSH uses the caller's normal host-key policy and authentication, optionally with
an agent or supplied master socket. A transaction creates and reuses a master when
needed. An SSH destination may be an alias, hostname or IP; it is not an implicit
IPv4 topology. Public tooling has no default password or personal key.

The target agent is package-owned. The controller requires protocol `wrtix-v2`
and the exact contract hash, then binds the installed engine and guard hashes to
each transaction. It does not silently overwrite the runtime on each deployment.
Install the compatible agent package first. There is one transaction path: agent
identity, resolver state, optional credential handling and reboot verification
cannot be disabled by a runtime-selection flag. Credentials and resolver policy
remain optional resources; omitting them preserves the existing configuration.

Version 0.2.0 does not accept v1 receipts or the removed runtime-selection
parameter. Finish pending transactions with their matching controller before
upgrading the package and controller together. This protocol version is independent
of any firmware release.

## Target transaction

`wrtix-agent` owns `/etc/wrtix`, its lock, ownership ledger, identity and rollback
journal. First registration snapshots affected baseline resources including the
previous unregistered state. Apply uses baseline comparison to reject concurrent
changes. Files, service policy and managed credentials are restored on failure.
Optional authentication omitted from configuration remains unmanaged.

A staged transaction has a bounded deadline. A guard survives controller loss;
required checks failing or being unavailable prevent confirmation. A planned MAC
change reboot has an explicit boot transition and resume window. Recovery cannot
extend the original transaction arbitrarily. Private receipts retain transaction
identity and recovery information without requiring the flake to evaluate again.

Checks run in `preflight`, `preconfirm` or `postrollback` phases. Target executables
are owned, hash-pinned files under `/usr/libexec/wrtix-managed/`; their input is
private, and execution is bounded. They are trusted root programs. The API does
not grant control of transaction deadlines or authentication, but it is not an OS
sandbox against malicious administrator-supplied code.

## Lifecycle boundaries

Package prerequisites are checked, not installed as part of a configuration
transaction. Package operations are blocked while recovery is pending. Agent
installation alone leaves the existing network and credentials unchanged.
The explicit `prepare-packages` phase installs missing prerequisites under a
separate preparation lock and persistent fence. APK commit hooks retain this
fence until preparation succeeds. Failed preparation can be retried after its
processes exit. Runtime identity checks for preparation permit this specific
fence, while configuration operations continue to reject it.
Sysupgrade backup includes declared managed state; it does not turn firmware
upgrades into atomic configuration transactions.

Platform-specific initial DHCP, recovery ports, defaults and boot migrations
belong to an image adapter. AP semantic and runtime checks belong to a user
extension. Neither is guessed by the public agent.
