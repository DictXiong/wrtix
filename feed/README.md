<!--
This file was generated entirely by AI.
It has not been reviewed line by line by a human.
-->
# OpenWrt feed

Add the **repository root** as an OpenWrt feed; package discovery finds
`feed/nixiz-agent/Makefile`. For a local checkout:

```text
src-link nixiz /absolute/path/to/nixiz
```

Then run `./scripts/feeds update nixiz`,
`./scripts/feeds install -p nixiz nixiz-agent`, select the package and build
`make package/nixiz-agent/compile`. This requires an SDK/build tree matching
the target OpenWrt release. The package contains architecture-independent
scripts; dependency and package-format compatibility still depend on the
release. The Makefile resolves the original checkout through feed symlinks
and packages `agent/` directly, without maintaining duplicate runtime files.

Installation does not change network or authentication configuration. An
upgrade/removal is refused while a transaction or restoration is pending.
A persistent package-operation marker blocks new transactions while files
are replaced. Successful installation validates packaged runtime hashes
before removing that marker. If installation fails, repair/reinstall the
package; do not remove the marker to bypass incomplete installation.

Removing the package stops only its own guard and retains configuration and
private state. A new firmware image must include a compatible `nixiz-agent`
package to continue managing retained configuration after sysupgrade.

## Lifecycle boundaries

The package installs no default password, authorized key, management address,
maintenance network, wireless interface or hardware driver. Install through an
existing authenticated administrator session; then use the controller's explicit
initialization operation to register the device. Registration and its affected
configuration baseline participate in the first transaction's rollback.

On OpenWrt 25.12, APK commit hooks enforce the lifecycle gate **before** package
files can change. A failing APK package script alone does not veto removal.
The global pre-commit hook therefore rejects any package commit while a nixiz
transaction is active; a post-commit hook validates the installed runtime and
contract before unblocking deployment. Do not bypass APK commit hooks. Root can
always bypass package policy; such operations are outside the transaction model.

The init guard also refuses normal stop, disable and restart while recovery is
pending. Package operations and configuration staging use the same lock, closing
the race between checking for an idle transaction and replacing runtime files.
The package operation marker remains after a failed operation. Repair the package
using a known compatible package version, and investigate the failure; deleting
its marker is not a supported recovery procedure.

The sysupgrade backup hook preserves the declared managed-file inventory and
identity, and refuses an active/incomplete transaction. It does not blindly copy
transaction archives or arbitrary private state directories. A package requirement
means “must already be installed”; configuration deployment does not install or
remove arbitrary packages and does not promise to roll back package databases or
firmware. Upgrade the firmware/package separately after completing the transaction.

This package has been exercised on the pinned official 25.12 x86_64 VM. Other
releases and package managers require their own lifecycle acceptance. `PKGARCH`
being `all` describes script architecture, not universal runtime compatibility.
