# This file was generated entirely by AI.
# It has not been reviewed line by line by a human.
# SPDX-License-Identifier: GPL-2.0-only
{ lib, ... }:
{
  imports = [ ./resources.nix ];
  options.resourceSets = lib.mkOption {
    type = lib.types.attrsOf (lib.types.submodule ./resources.nix);
    default = { };
    description = "Named producer-owned resource contributions. Names preserve ownership through compilation; direct resources remain caller-owned.";
  };
}
