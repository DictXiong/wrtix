# Usage

## Configure

Install a matching `wrtix-agent` package on the OpenWrt target from the signed
[release packages or OpenWrt feed](../feed/README.md).
Keep your existing SSH credentials and host-key verification. Installing the
package does not enroll the device or change its networking.

For NixOS, import `wrtix.nixosModules.default` and enable `services.openwrt`.
For ordinary Linux with Nix, expose a standalone output in your flake:

```nix
openwrtConfigurations.site = wrtix.lib.openwrtConfiguration {
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

Use `github:DictXiong/wrtix` as the input and retain `flake.lock` to pin its
revision. An explicit local `path:` input is suitable for development. The package is available for
`x86_64-linux` and `aarch64-linux`; the target agent is shell code packaged by an
OpenWrt feed.

UCI declarations own complete `/etc/config/<package>` files; they are not
imperative patches over whatever happens to be installed. Include the necessary
sections and hardware settings, or have a user profile generate them. The
[AP template](../templates/ap/ap.nix) illustrates a complete small network and
wireless configuration using ordinary resources, without a custom validator.

## Build and deploy

### Concise UCI declarations

`wrtix.lib.uci` accepts `package.type.name = options` and converts it to the same
typed resources used by profiles and explicit section definitions. Lists under a
type declare ordered anonymous sections. These declarations own each complete
UCI package file, so include all sections that should be present.

```nix
services.openwrt.devices.router = {
  target_host = "root@router.example";
  uci = wrtix.lib.uci {
    system.system = [ { hostname = "example-router"; timezone = "UTC"; } ];
  };
  services.system.reloadOn.uci = [ "system" ];
};
```

Use the explicit section form when you need to control ordering across different
section types. A duplicate named section across types is rejected. The AP
template uses this helper and provides complete network and wireless settings.

### Start from an existing router

Run this on the controller with existing SSH access. Capture works before
installing the agent. Values are written literally into the generated Nix module
by default. Select sensitive fields for encryption before building or committing
a captured configuration containing credentials.

```sh
openwrt-rebuild import --target-host root@router.example \
  --output ./imported-router
cd imported-router
nix run . -- build --flake .#imported
nix run . -- dry-run --flake .#imported --device router
```

The new directory is a complete standalone Nix flake project containing
`flake.nix` and `configuration.nix`. The latter is a reusable Nix module declaring
`services.openwrt` and the captured device. Add it to `modules` or `imports` when
incorporating the device into an existing project. The command refuses an
existing destination and pending unsaved UCI changes. It reads committed UCI
files and enabled matching init services.
Named/anonymous sections, lists and order are retained. Review service reloads,
hardware settings and the SSH endpoint before first enrollment. Declare extra
files, package requirements and root authentication explicitly when taking
ownership. Generated runtime files are outside this capture.

Repeat `--uci network --uci wireless` to capture selected packages. Import keeps
the original SSH port. `--device NAME` sets the enrollment identifier. Use
`--system aarch64-linux` for an ARM controller. Repeat `--secret-option` to
encrypt selected scalar or list options. Encryption requires `--sops-age` and
creates `secrets.yaml` containing only the selected values. Other options stay
readable in `configuration.nix`.

```sh
openwrt-rebuild import --target-host root@router.example --output ./imported-router \
  --secret-option wireless.default_radio0.key \
  --secret-option wireless.default_radio1.key \
  --sops-age age1YOUR_PUBLIC_RECIPIENT
```

Use the actual package, section and option names from the device. A selector
that matches no captured option is an error. Import without selections requires
no SOPS setup and produces only the two Nix files.
Multi-line UCI values are rejected because the resource renderer cannot
represent them. A failed encryption leaves no output project.
For development, `--wrtix-input path:/path/to/wrtix` selects a local checkout for
the generated project. Pair its controller with the matching agent contract.

Keep the generated lock file. Add reviewed Nix files and any encrypted YAML to
Git when using a Git-backed flake, and keep the age identity outside the
repository. Install the matching agent, then enroll through the normal
protected workflow.

```sh
nix run . -- switch --flake .#imported --device router \
  --init --target-host root@router.example
```

### Prepare required software

Declare prerequisites with `packages = [ "tcpdump" "iperf3" ];` and run the
explicit preparation step before deploying configurations that need them.

```sh
openwrt-rebuild prepare-packages --flake .#site --device router
openwrt-rebuild switch --flake .#site --device router
```

Preparation requires a matching agent and an idle configuration transaction. It
updates repository indexes and installs missing packages through APK or opkg.
Already installed packages are skipped. Package-manager operations can run
package scripts and alter persistent state, so they sit outside configuration
rollback. The command reports this boundary in progress and structured output.
It adds prerequisites and retains other installed packages.

A persistent preparation fence blocks configuration staging during installation.
APK hooks cooperate with this fence and reject replacement/removal of the agent.
An interrupted or failed preparation retains the fence. Retry the same command
to recheck the package state and clear it after success. A second preparation
cannot run concurrently. Normal `switch`, `test` and `verify` report missing
package names and stop before configuration mutation. The error includes a
copyable preparation command using the matching controller, built manifest,
device and SSH overrides. Run it, then retry the original deployment command.

Preparation uses public configuration and never resolves deployment secrets.
Agent installation and upgrade are explicit prerequisites to this workflow.

```sh
openwrt-rebuild build --flake .#openwrtConfigurations.site
openwrt-rebuild dry-run --flake .#openwrtConfigurations.site --device router
openwrt-rebuild switch --flake .#openwrtConfigurations.site --device router \
  --init --target-host root@router.example
```

`--init` / `--initialize` explicitly permits first registration and requires one
`--device` and an explicit `--target-host`. Later deployments omit `--init`.
`--target-host` overrides the initial SSH endpoint. Without an explicit
flake fragment, the wrapper uses the local hostname, following the NixOS-style
selection behavior. If a name exists in both namespaces, use
`nixosConfigurations.NAME` or `openwrtConfigurations.NAME` explicitly.

`test` applies a candidate under rollback protection; `switch` additionally
verifies and confirms it. Read the command's receipt path and final result.
For a DHCP address change, pass `--post-switch-host` when known, or
`--ask-post-switch-host` to enter the new endpoint after activation. A deadline is
still active while waiting for input. The tool verifies the transaction identity
at the new endpoint; an arbitrary responding host is not sufficient.

### Inspecting configuration differences

Use `--diff` to print a human-readable comparison on **stderr**. Standard output
keeps the existing JSON result and exit behavior: `verify` fails on differences,
while `dry-run --remote` succeeds when its read-only preview completes.

```sh
openwrt-rebuild verify --device router --diff
openwrt-rebuild dry-run --remote --device router --diff
openwrt-rebuild test --device router --diff
openwrt-rebuild switch --device router --diff
openwrt-rebuild confirm --receipt /path/to/receipt.json --diff
openwrt-rebuild switch --receipt /path/to/receipt.json --diff
openwrt-rebuild rollback --receipt /path/to/receipt.json --diff
```

Supply `--flake` as usual if you are not using the default configuration.
The underlying `openwrt-deploy` accepts the same flag on `verify`, `plan --remote`,
`deploy`, `confirm --receipt` and `rollback --receipt`.

For `test` and normal `switch`, the preview uses the same hash/mode snapshot as
the transaction baseline and is printed before staging or applying configuration.
A concurrent edit still causes staging to fail. `--diff` does not add an approval
prompt or extend the rollback deadline. Explicit receipt confirmation compares
against the saved candidate; rollback compares against the original backup.
Neither evaluates the current flake or decrypts its secrets. Cancelling a staged,
unapplied transaction has no configuration changes to display. Confirmation still
requires all existing readback, identity, deadline and extension checks.

The comparison shows file content, creation/deletion and mode changes, requested
service states, resolver symlink changes and root-password synchronization.
Binary changes have a summary; text diffs are limited to 2,000 lines per file.
There is no diff for an offline `dry-run`, `build` or `status`. `--quiet` hides
progress, but keeps explicitly requested diffs. `--diff` requires an updated
`wrtix-agent`; older installed agents continue to support operations without it.
The controller never replaces a package-owned agent to enable this feature.

Files derived from SOPS or provider references are hidden in their entirety,
including their old content. The provenance is saved in transaction metadata and
the managed-resource ledger, so receipt recovery and later deployments retain it.
Root credential resources are always private. There is no password-name heuristic:
literal credentials and secret data never declared through a reference cannot be
identified reliably and **may appear in a diff**. Use runtime secret references.
Old transaction journals without provenance hide file content conservatively;
released files without provenance are also hidden. A live `dry-run --remote --diff`
resolves runtime secrets to compare the actual target, unlike ordinary
`dry-run`; its JSON reports `secret_values_compared: true`. Secret values and file
contents are never added to stdout, receipts or public build products.

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

Confirmation reads fresh transaction status and uses its remaining uptime lease
as the SSH call budget, plus 15 seconds to receive the result. The grace does not
extend the target's deadline or relax either readback or required checks. This
applies to automatic confirmation and receipt recovery; `--wait` still controls
activation/reconnection, not confirmation. Invalid, expired or mismatched status
blocks confirmation. A client timeout remains a failure, not evidence of success.

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

### Other secret sources

Register a trusted controller program and use `secretRef` wherever a runtime
secret is accepted. The program is an immutable Nix artifact. This example reads
from a password store through its normal runtime authentication.

```nix
services.openwrt = {
  secretProviders.passwordStore = {
    executable = pkgs.writeShellScript "wrtix-password-store" ''
      set -eu
      request=$(${pkgs.coreutils}/bin/cat)
      key=$(printf '%s' "$request" | ${pkgs.jq}/bin/jq -er '.key')
      ${pkgs.pass}/bin/pass show "$key" | ${pkgs.jq}/bin/jq -Rs '{value: .}'
    '';
    timeoutSeconds = 30;
  };
  devices.router.files."/etc/application-token" = {
    mode = "0600";
    value.secretRef = { provider = "passwordStore"; key = "router/application-token"; };
  };
};
```

Providers receive a JSON object `{ "key": "...", "input": {} }` on stdin and
return exactly `{ "value": "a string" }` on stdout. Whitespace is preserved.
Use `input` for public lookup parameters. Credentials belong to the program's
runtime authentication environment. Provider output and stderr are suppressed
from deployment logs. Timeouts terminate the provider process group. Results are
cached per provider, key and input for one invocation, then discarded.

Build and preview only inspect references. Actual deployment resolves them and
runs the same final resource and extension checks as SOPS values. Ordinary
configuration requires no custom provider or validator.

### Changing the SSH port

`--port` selects the initial connection; `--post-switch-port` selects the fresh
connection used after activation or the planned reboot. Both accept 1–65535.
Omitting the latter keeps the initial port. Ports describe the controller's SSH
endpoint, which can differ from the listener port when using a forwarded port.
The port flag does not edit Dropbear: declare that change in UCI/resources too.

```sh
openwrt-rebuild switch --flake .#gateway --device router --initialize \
  --target-host 192.0.2.10 --port 22 --post-switch-port 12022
```

Combine it with `--post-switch-host` or `--ask-post-switch-host` when DHCP also
changes the address. A new private SSH connection must verify the original host
key and transaction before confirmation. An existing connection on the old port
cannot stand in for this verification. A failed new connection leaves rollback
armed under the original deadline.

Receipts retain the original endpoint, last verified endpoint and candidate port
separately. `status`/`rollback --receipt FILE` try saved endpoints, including the
old port after timeout rollback. `confirm --receipt FILE` verifies the candidate
port; use `--target-host` and `--port` to explicitly select a recovery endpoint.
A receipt confirmation may also specify `--post-switch-port` when correcting the
candidate endpoint. These operations require no flake or secret decryption.

The original trusted host-key alias is retained within the transaction. For an
unrelated later command at the new endpoint, normal OpenSSH known-host handling
still applies; the tool does not silently authorize a new known-host entry.
