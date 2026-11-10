# SPDX-License-Identifier: GPL-2.0-only
{ lib, ... }:
let
  inherit (lib) mkOption types;
  t = import ./types.nix { inherit lib; };
  absolute = types.strMatching "/.+";
in
{
  options = {
    uci = mkOption {
      type = types.attrsOf (types.attrsOf t.section);
      default = { };
      description = "Named UCI packages and sections. Conflicting module definitions fail unless explicitly overridden.";
    };
    files = mkOption {
      type = types.attrsOf t.file;
      default = { };
    };
    services = mkOption {
      type = types.attrsOf t.service;
      default = { };
    };
    packages = mkOption {
      type = types.listOf (types.strMatching "[A-Za-z0-9][A-Za-z0-9+_.-]*");
      default = [ ];
      description = "Required packages. prepare-packages installs missing prerequisites independently before the configuration transaction.";
    };
    users.root = {
      passwordHash = mkOption {
        type = types.nullOr t.secret;
        default = null;
        description = "Runtime secret reference to a crypt password hash; null preserves the current password.";
      };
      authorizedKeys = mkOption {
        type = types.nullOr (types.listOf types.str);
        default = null;
        description = "Explicit public SSH authorization; null preserves existing authorization.";
      };
    };
    resolver = mkOption {
      type = types.nullOr (
        types.submodule {
          options.target = mkOption { type = absolute; };
        }
      );
      default = null;
      description = "Optional /etc/resolv.conf symlink target; null leaves the resolver unchanged.";
    };
    identity = mkOption {
      type = types.attrs;
      default = { };
      description = "Optional expected device facts; no network topology or address is required.";
    };
    ownership = {
      exclusivePackages = mkOption {
        type = types.listOf t.token;
        default = [ ];
      };
      protectedPackages = mkOption {
        type = types.listOf t.token;
        default = [ ];
      };
      protectedOptions = mkOption {
        type = types.attrsOf (types.listOf t.token);
        default = { };
      };
      requiredServices = mkOption {
        type = types.listOf t.token;
        default = [ ];
      };
      forbiddenServices = mkOption {
        type = types.listOf t.token;
        default = [ ];
      };
    };
    overrides.uci = mkOption {
      type = types.attrsOf (types.attrsOf t.sectionOverride);
      default = { };
    };
    checks = mkOption {
      default = { };
      type = types.attrsOf (
        types.submodule {
          options = {
            phase = mkOption {
              type = types.enum [
                "compile"
                "preflight"
                "preconfirm"
                "postrollback"
              ];
            };
            executable = mkOption { type = absolute; };
            timeoutSeconds = mkOption {
              type = types.ints.between 1 120;
              default = 30;
            };
            input = mkOption {
              type = types.attrs;
              default = { };
              description = "Structured private input; SOPS references resolve only during deployment.";
            };
          };
        }
      );
      description = "Named required checks. Compile checks execute on the controller; other phases execute on the target.";
    };
  };
}
