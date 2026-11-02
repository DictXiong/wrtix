# This file was generated entirely by AI.
# It has not been reviewed line by line by a human.
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
