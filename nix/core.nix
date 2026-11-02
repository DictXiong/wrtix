# This file was generated entirely by AI.
# It has not been reviewed line by line by a human.
# SPDX-License-Identifier: GPL-2.0-only
{
  config,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib) mkOption types;
  cfg = config.services.openwrt;
  weaken =
    value:
    if builtins.isAttrs value && !(value ? _type) && !lib.isDerivation value then
      lib.mapAttrs (_: weaken) value
    else
      lib.mkOverride 900 value;
  resourceNames = [
    "uci"
    "files"
    "services"
    "packages"
    "users"
    "resolver"
    "identity"
    "ownership"
    "overrides"
    "checks"
    "resourceSets"
  ];
  resources = value: lib.getAttrs resourceNames value;
  deviceType = types.submoduleWith {
    specialArgs = { inherit pkgs; };
    modules = [
      ./device.nix
      { config = weaken cfg.defaults; }
    ]
    ++ cfg.deviceModules;
  };
  base = lib.filterAttrs (_: device: device.enable) (if cfg.enable then cfg.devices else { });
  generated = lib.evalModules {
    specialArgs = {
      inherit pkgs;
      fleet = base;
    };
    modules = [
      {
        options.devices = mkOption {
          type = types.attrsOf types.deferredModule;
          default = { };
          description = "Additional resources for already declared devices, computed from the base fleet.";
        };
        options.assertions = mkOption {
          type = types.listOf types.attrs;
          default = [ ];
        };
      }
    ]
    ++ lib.optionals cfg.enable cfg.fleetModules;
  };
  normalize =
    value:
    if builtins.isPath value || lib.isDerivation value then
      "${value}"
    else if builtins.isAttrs value then
      lib.mapAttrs (_: normalize) value
    else if builtins.isList value then
      map normalize value
    else
      value;
  final = lib.mapAttrs (
    name: device:
    let
      merged = lib.evalModules {
        specialArgs = { inherit pkgs; };
        modules = [
          ./resource-container.nix
          {
            _file = "nixiz base device ${name}";
            config = resources device;
          }
          (generated.config.devices.${name} or { })
        ];
      };
    in
    {
      inherit (device) id target_host ssh_port;
    }
    // merged.config
  ) base;
  assertions = [
    {
      assertion = lib.all (name: builtins.hasAttr name base) (
        builtins.attrNames generated.config.devices
      );
      message = "A fleet module generated resources for an undeclared or disabled device.";
    }
    {
      assertion =
        let
          ids = map (device: device.id) (builtins.attrValues base);
        in
        builtins.length ids == builtins.length (lib.unique ids);
      message = "OpenWrt device enrollment identifiers must be unique within the fleet.";
    }
  ]
  ++ generated.config.assertions
  ++ lib.concatMap (device: device.assertions) (builtins.attrValues base);
  failed = builtins.filter (entry: !entry.assertion) assertions;
  checked =
    value:
    if failed == [ ] then
      value
    else
      throw (
        "OpenWrt configuration assertions failed:\n"
        + lib.concatMapStringsSep "\n" (entry: entry.message) failed
      );
in
{
  options.services.openwrt = {
    enable = lib.mkEnableOption "declarative OpenWrt configuration management";
    defaults = mkOption {
      type = types.attrsOf types.anything;
      default = { };
      description = "Shared device defaults; validated by each device's typed module interface.";
    };
    deviceModules = mkOption {
      type = types.listOf types.deferredModule;
      default = [ ];
      description = "User modules declaring typed profiles and producing ordinary device resources.";
    };
    fleetModules = mkOption {
      type = types.listOf types.deferredModule;
      default = [ ];
      description = "Modules receiving the finalized base fleet and producing resources for its devices. Outputs are not fed back into the base fleet.";
    };
    devices = mkOption {
      type = types.attrsOf deviceType;
      default = { };
    };
    deployment = {
      rollbackTimeout = mkOption {
        type = types.ints.between 60 7200;
        default = 600;
      };
      port = mkOption {
        type = types.nullOr types.port;
        default = null;
      };
      sshOptions = mkOption {
        type = types.listOf types.str;
        default = [ ];
      };
    };
    decryption = {
      keyFile = mkOption {
        type = types.nullOr types.str;
        default = null;
      };
      sshKeyFile = mkOption {
        type = types.nullOr types.str;
        default = null;
      };
      sshKeyFiles = mkOption {
        type = types.listOf types.str;
        default = [ ];
      };
      gnupgHome = mkOption {
        type = types.nullOr types.str;
        default = null;
      };
      gnupgSshKeyFiles = mkOption {
        type = types.listOf types.str;
        default = [ ];
      };
    };
    package = mkOption {
      type = types.package;
      default = pkgs.callPackage ./package.nix { };
    };
    manifest = mkOption {
      type = types.attrs;
      readOnly = true;
    };
    manifestFile = mkOption {
      type = types.path;
      readOnly = true;
    };
  };
  config = lib.mkMerge [
    {
      services.openwrt = {
        manifest = checked {
          schema_version = 1;
          kind = "openwrt-fleet";
          inherit (cfg) deployment decryption;
          devices = normalize final;
        };
        manifestFile = pkgs.writeText "openwrt-fleet.json" (builtins.toJSON cfg.manifest);
      };
    }
    (lib.mkIf cfg.enable {
      system.build = {
        openwrt-manifest = cfg.manifestFile;
        openwrt-deploy = pkgs.writeShellScriptBin "openwrt-deploy" ''
          exec ${cfg.package}/bin/openwrt-deploy --manifest ${cfg.manifestFile} "$@"
        '';
        openwrt-rebuild = pkgs.writeShellScriptBin "openwrt-rebuild" ''
          exec ${cfg.package}/bin/openwrt-rebuild "$@"
        '';
      };
    })
  ];
}
