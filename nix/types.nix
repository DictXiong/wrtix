# This file was generated entirely by AI.
# It has not been reviewed line by line by a human.
# SPDX-License-Identifier: GPL-2.0-only
{ lib }:
let
  inherit (lib) types mkOption;
  nonempty = types.strMatching ".+";
  token = types.strMatching "[A-Za-z0-9_]+";
  secret = types.submodule (
    { config, ... }: {
      options.sops = {
        file = mkOption {
          type = types.path;
          description = "Encrypted SOPS source; never a decrypted secret file.";
        };
        key = mkOption {
          type = types.strMatching "[^/]+(/[^/]+)*";
          description = "SOPS slash-separated key path, as a string.";
        };
        format = mkOption {
          type = types.enum [
            "yaml"
            "json"
          ];
          default = if lib.hasSuffix ".json" (toString config.sops.file) then "json" else "yaml";
        };
      };
    }
  );
  scalar = types.oneOf [
    types.str
    types.int
    types.bool
    secret
  ];
  value = types.either scalar (types.listOf scalar);
  section = types.submodule {
    options = {
      type = mkOption { type = types.strMatching "[A-Za-z0-9_-]+"; };
      anonymous = mkOption {
        type = types.bool;
        default = false;
      };
      order = mkOption {
        type = types.int;
        default = 0;
      };
      options = mkOption {
        type = types.attrsOf value;
        default = { };
      };
    };
  };
  sectionOverride = types.submodule {
    options = {
      options = mkOption {
        type = types.attrsOf value;
        default = { };
      };
      removeOptions = mkOption {
        type = types.listOf token;
        default = [ ];
      };
    };
  };
  file = types.submodule {
    options = {
      format = mkOption {
        type = types.enum [
          "text"
          "json"
          "yaml"
        ];
        default = "text";
      };
      text = mkOption {
        type = types.nullOr types.str;
        default = null;
      };
      source = mkOption {
        type = types.nullOr types.path;
        default = null;
      };
      value = mkOption {
        type = types.anything;
        default = null;
        description = "Structured JSON/YAML data; SOPS references resolve only at deployment.";
      };
      mode = mkOption {
        type = types.enum [
          "0600"
          "0644"
          "0700"
          "0755"
          "600"
          "644"
          "700"
          "755"
        ];
        default = "0644";
      };
    };
  };
  service = types.submodule {
    options = {
      enable = mkOption {
        type = types.bool;
        default = true;
      };
      running = mkOption {
        type = types.nullOr types.bool;
        default = null;
        description = "Expected persistent process state after applying the action; null permits one-shot services.";
      };
      action = mkOption {
        type = types.enum [
          "reload"
          "restart"
        ];
        default = "reload";
      };
      reloadOn = {
        uci = mkOption {
          type = types.listOf token;
          default = [ ];
        };
        files = mkOption {
          type = types.listOf nonempty;
          default = [ ];
        };
      };
    };
  };
in
{
  inherit
    nonempty
    token
    secret
    scalar
    value
    section
    sectionOverride
    file
    service
    ;
}
