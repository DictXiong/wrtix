# SPDX-License-Identifier: GPL-2.0-only
# A user-supplied typed profile; wrtix does not know its name or semantics.
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
