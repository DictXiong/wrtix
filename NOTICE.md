# License and provenance

wrtix is distributed under GPL-2.0-only; see [COPYING](COPYING).
COPYING includes the complete GPL version 2 text, copied without modification
from the upstream `LICENSES/GPL-2.0` license-text section. The inherited upstream
COPYING notice declared GPL-2.0-only and referred to that file; the license choice
is unchanged.

The initial controller, SSH handling, progress and receipt code, secret resolver,
transaction engine and regression cases are derived from the OpenWrt tooling in
DictXiong's NixOS-Config, revision
`22d39fe682f97bee4eccffcc533a9f8edc88c7f1`. Private inventory, credentials,
evidence and AP policy are excluded. Public resource interfaces and target
package integration were generalized for this project.

The license text and limited generic enrollment validation concepts derive from
[ImmortalWrt-for-Gemtek-brightspeed](https://github.com/DictXiong/ImmortalWrt-for-Gemtek-brightspeed),
revision `3f3072e4baacdc27234c8510f15ddf7c5ba13da5`. Hardware drivers, factory
network policy and AP-specific configuration generators are not included.

Existing copyright, license and upstream source notices are preserved. The
official image checksum in `tests/vm/image.json` is verified against the upstream
download manifest; it is not an invented fixture.
