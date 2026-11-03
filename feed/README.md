# OpenWrt target installation

## Install a release package

The controller runs Nix; the router only needs `wrtix-agent` and its OpenWrt
package dependencies. Start with OpenWrt 25.12 and APK 3. The agent contains
architecture-independent scripts, but other OpenWrt releases and opkg/IPK
have not received the same lifecycle acceptance.

On a trusted controller with GitHub CLI, download the release assets and verify
their checksums and GitHub Actions provenance:

```sh
mkdir wrtix-release && cd wrtix-release
gh release download v1.0.0-alpha.1 --repo DictXiong/wrtix
sha256sum --check SHA256SUMS
gh attestation verify wrtix-agent-1.0.0_alpha1-r1.apk --repo DictXiong/wrtix
gh attestation verify wrtix-v1.0.0-alpha.1.pem --repo DictXiong/wrtix
```

Each build uses a release-specific signing key. The private key is discarded;
only the public key is distributed. Authenticate that public key through the
release provenance before trusting it. A future release can have a different
key; verify and install that release's key again. SHA-256 alone detects changes
but does not establish publisher identity.

Copy the APK and verified public key to your router through an authenticated
administrator session (adjust host and SSH options to your device):

```sh
scp wrtix-agent-1.0.0_alpha1-r1.apk wrtix-v1.0.0-alpha.1.pem root@router.example:/tmp/
ssh root@router.example
mkdir -p /etc/apk/keys
cp /tmp/wrtix-v1.0.0-alpha.1.pem /etc/apk/keys/
apk update
apk add /tmp/wrtix-agent-1.0.0_alpha1-r1.apk
```

No `--allow-untrusted` is required. Installation validates the package signature
and resolves its dependencies through the router's configured repositories. It
does not change the network, password or authorized keys, and does not register
the device. Use the matching controller and explicitly run `switch --init` for
first enrollment; see [usage](../docs/usage.md). The controller checks protocol
compatibility rather than assuming that similarly named versions are compatible.

## Build through an OpenWrt feed

Add the **repository root** as an OpenWrt feed; package discovery finds
`feed/wrtix-agent/Makefile`. In the matching OpenWrt SDK/build tree's
`feeds.conf`, pin a reviewed commit (replace `<commit>`):

```text
src-git wrtix https://github.com/DictXiong/wrtix.git^<commit>
```

For a local checkout instead:

```text
src-link wrtix /absolute/path/to/wrtix
```

Then run `./scripts/feeds update wrtix`,
`./scripts/feeds install -p wrtix wrtix-agent`, select the package and build
`make package/wrtix-agent/compile`. This requires an SDK/build tree matching
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
private state. A new firmware image must include a compatible `wrtix-agent`
package to continue managing retained configuration after sysupgrade.

## Lifecycle boundaries

The package installs no default password, authorized key, management address,
maintenance network, wireless interface or hardware driver. Install through an
existing authenticated administrator session; then use the controller's explicit
initialization operation to register the device. Registration and its affected
configuration baseline participate in the first transaction's rollback.

On OpenWrt 25.12, APK commit hooks enforce the lifecycle gate **before** package
files can change. A failing APK package script alone does not veto removal.
The global pre-commit hook therefore rejects any package commit while a wrtix
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

## Automated releases

`VERSION` supplies the Nix package version and OpenWrt package version; APK
prereleases use its native spelling, for example `1.0.0_alpha1-r1` for
`v1.0.0-alpha.1`. `ci/openwrt-sdk.json` fixes the official SDK checksum and the
base and dependency feed revisions. `ci/build-package.py` builds through that SDK and
exports only the APK, its signed package index, public signing key, compatibility
metadata and checksums.

CI runs the module/controller checks and builds the APK on pushes and pull
requests. The actual signed artifact must then pass the isolated official VM's
package and controller acceptance. A matching `v<VERSION>` tag publishes a
pre-release only after all three jobs pass, and creates GitHub provenance
attestations for the release assets. CI never publishes raw VM credentials or
controller receipts. Add public release notes at `docs/releases/<tag>.md` before
tagging. Existing releases are not overwritten.
