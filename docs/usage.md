<!--
This file was generated entirely by AI.
It has not been reviewed line by line by a human.
-->
# Usage

## Configure

Install a matching `nixiz-agent` package on the OpenWrt target through the feed.
Keep your existing SSH credentials and host-key verification. Installing the
package does not enroll the device or change its networking.

For NixOS, import `nixiz.nixosModules.default` and enable `services.openwrt`.
For ordinary Linux with Nix, expose a standalone output in your flake:

```nix
openwrtConfigurations.site = nixiz.lib.openwrtConfiguration {
  pkgs = nixpkgs.legacyPackages.x86_64-linux;
  modules = [
    {
      services.openwrt = {
        enable = true;
        devices.router = {
          target_host = "root@router.example";
          files."/etc/site-label" = {
            text = "Example site\n";
            mode = "0644";
          };
        };
      };
    }
  ];
};
```

Use a pinned flake input for nixiz. An explicit local `path:` input is suitable for
development before publishing a repository. The package is available for
`x86_64-linux` and `aarch64-linux`; the target agent is shell code packaged by an
OpenWrt feed.

## Build and deploy

```sh
openwrt-rebuild build --flake .#openwrtConfigurations.site
openwrt-rebuild dry-run --flake .#openwrtConfigurations.site --device router
openwrt-rebuild switch --flake .#openwrtConfigurations.site --device router --init
```

`--init` / `--initialize` explicitly permits first registration. Later deployments
omit it. `--target-host` overrides the initial SSH endpoint. Without an explicit
flake fragment, the wrapper uses the local hostname, following the NixOS-style
selection behavior. If a name exists in both namespaces, use
`nixosConfigurations.NAME` or `openwrtConfigurations.NAME` explicitly.

`test` applies a candidate under rollback protection; `switch` additionally
verifies and confirms it. Read the command's receipt path and final result.
For a DHCP address change, pass `--post-switch-host` when known, or
`--ask-post-switch-host` to enter the new endpoint after activation. A deadline is
still active while waiting for input. The tool verifies the transaction identity
at the new endpoint; an arbitrary responding host is not sufficient.

## Secrets and recovery

Secrets are runtime SOPS references, never literal passwords in Nix expressions.
The decryption identity belongs to the executing controller; no NixOS switch is
required to decrypt for a deployment. Shared references can serve multiple
devices. Optional root authentication preserves existing credentials when absent.

Receipts are private files in the per-user state directory, not in the current
working tree or build closure. `openwrt-rebuild receipts` lists them. Recovery
commands accept `--receipt`; inspect `openwrt-rebuild --help` for the available
confirm, rollback, status and cleanup operations. Keep the receipt until the
transaction has completed and you have verified access.

A missing required check, changed target identity, runtime mismatch or unsafe
host key blocks the operation. Resolve the cause rather than disabling checks.
Firmware corruption, physical disconnection and arbitrary package mutations are
outside configuration rollback guarantees.

A runtime reference uses a single slash-separated key string:

```nix
services.openwrt.decryption.sshKeyFiles = [ "/etc/ssh/ssh_host_ed25519_key" ];
services.openwrt.devices.router.users.root.passwordHash.sops = {
  file = ./secrets.yaml; # encrypted file, copied to the store as ciphertext
  key = "router/root_password_hash";
};
```

That value must be a crypt password hash, not a plaintext login password. For
third-party JSON or YAML files, put references inside `files.<path>.value` and
select `format = "json"` or `"yaml"`. Use restrictive file modes for secrets.
The same encrypted source and key may be referenced by multiple devices.
