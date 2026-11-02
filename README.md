<!--
This file was generated entirely by AI.
It has not been reviewed line by line by a human.
-->
# nixiz

Declarative OpenWrt configuration with Nix, authenticated SSH deployment and
confirmed configuration transactions.

nixiz provides typed resources, user-defined profile modules, runtime SOPS secret
resolution and an OpenWrt target package. It does not prescribe a router topology,
wireless policy, management subnet or initial credentials. It can manage a device
without wireless, a bridge or custom MAC addresses.

- [Architecture](docs/architecture.md): boundaries, ownership and transactions.
- [Usage](docs/usage.md): configuration, deployment and recovery.
- [Extensions](docs/extensions.md): typed profiles and required checks.
- [OpenWrt package](feed/README.md): target installation and lifecycle.
- [Testing and limits](docs/testing.md): regression coverage and official VM acceptance.
- [Development rules](AGENTS.md) and [source attribution](NOTICE.md).

This initial implementation is experimental. Do not interpret module or unit
tests as proof of hardware compatibility. Configuration rollback does not
roll back firmware, package installations or arbitrary extension side effects.
