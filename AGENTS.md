# Development rules

wrtix manages declared OpenWrt resources. Keep topology, hardware policy, address
allocation, wireless presets and personal defaults in consumer modules. A user
profile must compile through the same ownership and validation pipeline as direct
resources. Never add a built-in profile to accommodate one downstream device.

Preserve independent working changes. Split commits by responsibility; never
commit another repository or publish without authorization. Do not store real
inventory, credentials, packet captures or deployment evidence here. Use synthetic
fixtures and documentation networks. All `evidence/` directories are ignored.

Transactions must have one recovery owner, immutable deadlines, authenticated
identity and fail-closed required checks. Do not relax checks to make tests pass.
Package lifecycle and firmware upgrades are distinct from configuration rollback.
Extensions are trusted administrative code, not sandboxed untrusted plugins.

Use standard Nix modules and immutable build inputs. Secrets resolve at deployment
time and must not enter the Nix store, process arguments or logs. Version the
public contract independently from any firmware. Preserve source attribution.

Run the affected tests, then `nix flake check` before release. VM tests are separate
and require KVM and network access. Test results must distinguish pure evaluation,
simulated target execution, official OpenWrt VM and physical device validation.
Documentation describes supported behavior and limitations, not work diaries.

Keep the project-wide AI assistance disclosure in README.md only; do not add
per-file AI-generation headers. Preserve copyright, license and upstream source
notices. Generated machine-readable data must not carry AI attribution fields.
