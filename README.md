# wrtix

Declarative OpenWrt configuration with Nix, authenticated SSH deployment and
confirmed configuration transactions.

wrtix provides typed resources, user-defined profile modules, runtime SOPS secret
resolution and an OpenWrt target package. It does not prescribe a router topology,
wireless policy, management subnet or initial credentials. It can manage a device
without wireless, a bridge or custom MAC addresses.

Existing routers can be captured with `openwrt-rebuild import`. The generated
project contains readable UCI values, with optional encryption for selected
fields. Review it before deployment. `prepare-packages` installs missing prerequisites as an independent
step. See [usage](docs/usage.md) for capture, concise UCI configuration and secret
provider examples.

## Quick start: a wired Wi-Fi access point

This example turns a standard OpenWrt LAN into a single-radio AP. The upstream
router supplies DHCP, while OpenWrt bridges Wi-Fi clients to the declared LAN ports
and obtains its own management address by DHCP. Existing SSH credentials are
preserved. **No custom profile or validator is needed.**

Prerequisites: Linux with Nix flakes enabled, SSH access, and a matching
[`wrtix-agent` package](feed/README.md) installed on OpenWrt. Download the signed OpenWrt 25.12 APK from
[Releases](https://github.com/DictXiong/wrtix/releases), or build it through the
OpenWrt feed. It is not yet in the official package repositories.
The example targets standard OpenWrt with individually named LAN devices, the
usual LAN firewall zone, and a 2.4GHz mac80211 radio. Check the actual port and
PHY names first; the sample values are not hardware detection.

```sh
mkdir my-routers && cd my-routers
nix flake init --template github:DictXiong/wrtix#ap
```

Edit the small settings block in the generated `flake.nix`:

```nix
(import ./ap.nix {
  lib = wrtix.lib;
  targetHost = "root@router.example";
  ssid = "Example Wi-Fi";
  passwordFile = ./secrets.yaml;
  ports = [ "lan1" "lan2" ]; # Include the uplink and maintenance ports.
  phy = "phy0";              # Select your 2.4GHz PHY.
  country = "GB";            # Use your actual regulatory country.
})
```

The included [ap.nix](templates/ap/ap.nix) is ordinary UCI and service declarations,
not a built-in AP subsystem. It defines loopback, a LAN bridge with DHCP
management, and one WPA2-PSK hotspot using automatic channel selection and 20MHz.
It disables the local DHCP/RA services and uses DNS learned from upstream.
**It owns and replaces the complete `network` and `wireless` UCI files**: existing
WAN, VLAN and extra-radio definitions are not retained. Use a listed **LAN port**
for the upstream cable and include your maintenance port in `ports`. Back up any
existing configuration you need to keep. Use `aarch64-linux` in `flake.nix` for an
ARM controller.

### Store the Wi-Fi password

Create `secrets.yaml` with SOPS, containing a `wifi_password` string of 8–63
characters. If you already use SOPS, use your existing recipient and runtime
identity. Otherwise, this one-time age setup keeps the private key outside the
configuration directory:

```sh
nix shell nixpkgs#age nixpkgs#sops
export SOPS_AGE_KEY_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/sops/age/keys.txt"
mkdir -p -m 700 "$(dirname "$SOPS_AGE_KEY_FILE")"
test -e "$SOPS_AGE_KEY_FILE" || age-keygen -o "$SOPS_AGE_KEY_FILE"
sops --age "$(age-keygen -y "$SOPS_AGE_KEY_FILE")" secrets.yaml
```

In the SOPS editor, set `wifi_password` to your chosen password and save. SOPS
writes ciphertext; do not put the plaintext password in Nix. wrtix decrypts it
only at deployment time. If this directory is in Git, add `flake.nix`, `ap.nix`
and the **encrypted** `secrets.yaml` so Nix can see them; never add the age key.

### Build and switch

```sh
nix run . -- build --flake .#home
nix run . -- dry-run --flake .#home --device router
nix run . -- switch --flake .#home --device router \
  --init --target-host root@router.example --ask-post-switch-host
```

Replace the example SSH address in the command too. `--init` explicitly permits
first enrollment; omit it on later deployments. Prepare through a maintenance
connection and avoid connecting two active DHCP servers: connect the upstream
LAN cable after the candidate has disabled OpenWrt's DHCP server. At the address
prompt, look up the upstream router's DHCP lease and enter the AP's new SSH
address. The original rollback deadline stays active while you do so; failed
verification or missing confirmation restores the affected configuration.

The template uses the same locked wrtix version for its controller and
configuration. SSH uses your existing credentials or agent and asks before
trusting an unknown host key. For NixOS, import `wrtix.nixosModules.default` and
use the same `services.openwrt` resources; the two CLI commands are installed
automatically. The `#minimal` template demonstrates managing just one file,
without a network change or any secret setup.

## Documentation

- [Architecture](docs/architecture.md): boundaries, ownership and transactions.
- [Usage](docs/usage.md): configuration, deployment and recovery.
- [Extensions](docs/extensions.md): typed profiles and required checks.
- [OpenWrt package](feed/README.md): target installation and lifecycle.
- [Testing and limits](docs/testing.md): regression coverage and official VM acceptance.
- [Development rules](AGENTS.md) and [source attribution](NOTICE.md).

This initial implementation is experimental. Do not interpret module or unit
tests as proof of hardware compatibility. Configuration rollback does not
roll back firmware, package installations or arbitrary extension side effects.

This project is developed with AI-assisted programming.

Licensed under GPL-2.0-only; see [COPYING](COPYING) and [NOTICE](NOTICE.md).
