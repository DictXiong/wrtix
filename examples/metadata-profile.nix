# This file was generated entirely by AI.
# It has not been reviewed line by line by a human.
# SPDX-License-Identifier: GPL-2.0-only
# A user-supplied typed profile; nixiz does not know its name or semantics.
{ config, lib, ... }:
{
  options.profiles.metadata = {
    enable = lib.mkEnableOption "an example managed metadata file";
    label = lib.mkOption {
      type = lib.types.str;
      default = "example";
    };
  };
  config = lib.mkIf config.profiles.metadata.enable {
    resourceSets.metadata.files."/etc/example-label" = {
      text = config.profiles.metadata.label;
      mode = "0644";
    };
  };
}
