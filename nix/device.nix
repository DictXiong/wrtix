# SPDX-License-Identifier: GPL-2.0-only
{ lib, name, ... }:
let
  inherit (lib) mkOption types;
in
{
  imports = [ ./resource-container.nix ];
  options = {
    enable = mkOption {
      type = types.bool;
      default = true;
    };
    id = mkOption {
      type = types.strMatching "[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}";
      default = name;
      readOnly = true;
      description = "Persistent enrollment identifier derived from the device attribute name, independent of network addresses.";
    };
    target_host = mkOption {
      type = types.strMatching "[^[:space:]]+";
      default = name;
      description = "SSH destination, such as an alias, hostname, IPv4 or IPv6 address.";
    };
    ssh_port = mkOption {
      type = types.port;
      default = 22;
    };
    assertions = mkOption {
      type = types.listOf (
        types.submodule {
          options = {
            assertion = mkOption { type = types.bool; };
            message = mkOption { type = types.str; };
          };
        }
      );
      default = [ ];
      internal = true;
    };
    # Users add typed profiles.<name> options through deviceModules. There is
    # deliberately no freeform profile type that could swallow misspellings.
  };
}
