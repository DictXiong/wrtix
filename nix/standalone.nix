# SPDX-License-Identifier: GPL-2.0-only
{ lib, ... }:
{
  imports = [ ./core.nix ];
  options.system.build = lib.mkOption {
    type = lib.types.attrsOf lib.types.package;
    default = { };
    internal = true;
  };
}
