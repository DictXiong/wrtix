# Extending wrtix

## Typed profiles

Register ordinary Nix modules in `services.openwrt.deviceModules`. The module
receives a device's `config`, `lib` and `pkgs` and declares options under
`profiles.<name>`. There is no profile registry or built-in list of names. See
[metadata-profile.nix](../examples/metadata-profile.nix) for a minimal example.

Generate resources in an owner-named `resourceSets.<owner>` contribution. This
keeps provenance until compilation and makes collisions with direct resources or
other extensions explicit. Use device `assertions` for semantic constraints.
Do not hide complete UCI configurations in shell scripts or bypass final checks.

Shared values go in `services.openwrt.defaults`; per-device declarations override
those defaults through the standard Nix priority mechanism. Unknown profile
options fail evaluation even though the profile namespace is extensible. Disabling
a profile removes its generated resources from the desired ownership set. The
next transaction reconciles formerly owned files, with a rollback baseline;
it does not recursively delete arbitrary unmanaged directories.

## Cross-device generation

A `fleetModules` module receives `fleet`, the enabled base devices after their
defaults and device modules have been evaluated. Declare
`config.devices.<name>` with additional generic resources, or assertions for the
whole fleet. References to undeclared or disabled devices fail evaluation.

The cross-device output does not feed back into `fleet`. Compute deterministic
address tables or equivalent associations once from that base input, then derive
all peer resources from the same table. wrtix itself assigns no addresses and
knows no neighbor or radio policy.

## Ownership and overrides

Nix definition priority and resource ownership are separate. `mkDefault` and
`mkForce` resolve definitions inside a Nix module option; they are not permission
to overwrite an exclusively owned resource from another producer. Resource
claims, explicit overrides and final checks remain effective after module
merging. Keep related output in a consistently named resource set.

Resources should have one semantic owner. If two modules disagree on a resource,
choose a single producer or express an allowed explicit override. Never rely on
module import order. A replacement must still pass the resource validators and
all required extension checks.

## Service activation and acceptance

`services.<name>.running` controls start/stop intent and retains its existing
one-shot and rollback semantics. `checkRunning` defaults to `true`; set it to
`false` to accept matching files and service enablement without requiring the
candidate daemon to remain healthy. Enable, disable, start, stop and reload
commands still run when needed, and their failures still fail deployment.
Rollback continues to restore and verify the actual baseline process state.
Explicit required checks remain mandatory, regardless of `checkRunning`.

This policy requires matching controller and agent packages: the contract carries
`service_runtime_check_policy`. Older agents are rejected before mutation rather
than silently applying different acceptance rules. Complete pending transactions
with their original controller before upgrading the agent.

## Required checks

Custom checks are optional. Direct resource configurations need no validator or
check executable: wrtix already validates resource types, ownership, paths,
permissions and transaction consistency. A simple profile can use Nix
`assertions` alone. Add executable checks only when a constraint requires
resolved secrets, final merged resources or live device state.

A check has a unique name, `phase`, absolute `executable`, `timeoutSeconds` and
an attribute-set `input`. Timeouts are bounded; every declared check is required.

- `compile`: controller executable supplied as an immutable Nix artifact. It
  receives `{ input, device_id, resources, fleet }` on stdin, after secret
  resolution and resource merging. `resources.uci` is the final structured UCI;
  `resources.files` contains base64 content and modes. Fleet entries contain
  final identity, UCI, files and services.
- `preflight`: target check before baseline staging and configuration mutation.
- `preconfirm`: target check before accepting the candidate; also runs during
  explicit verification and noop validation.
- `postrollback`: target check during restoration. Failure leaves recovery
  incomplete rather than reporting success.

Target executables must be owned files under `/usr/libexec/wrtix-managed/`, mode
`0700` or `0755`, with the expected digest. They receive only their resolved
`input` as stdin. Package requirements must explicitly include any tools they
need. A missing binary or timed-out check is a failure.

Use exit status for success or failure; output is suppressed to avoid leaking
secrets. Put secret references in private input, never in executable arguments,
Nix-generated scripts or public derivations. Checks must be read-only and must
not change journals, deadlines, authentication or recovery services. They run as
trusted administrative code, so this interface is not an adversarial sandbox.

AP-specific relationships (MAC/MLD/BSSID, SSID security, capacity, roaming and
hardware acceleration) belong in an external profile and its required checks.
A routing or VPN profile can use the same interface without importing AP code.

## Runtime secret providers

Register controller executables under `services.openwrt.secretProviders` and
reference them with `{ secretRef = { provider = "name"; key = "lookup"; }; }`.
The same reference works in UCI values, structured files, root password hashes
and check input. Providers receive public lookup input on stdin and return a
single private string in a JSON `value` field. See the [provider example](usage.md#other-secret-sources).

Provider programs are trusted administrative code. Ship executable logic as a
Nix artifact and obtain credentials at runtime. Build and preview perform no
lookup. One invocation resolves each provider/key/input combination once before
resource validation. A missing provider, invalid response or timeout prevents
deployment. Stderr and invalid output stay out of logs, and timeout cleanup
terminates the provider process group.
