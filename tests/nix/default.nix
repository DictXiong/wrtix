# This file was generated entirely by AI.
# It has not been reviewed line by line by a human.
# SPDX-License-Identifier: GPL-2.0-only
{ pkgs, nixiz }:
let
  lib = pkgs.lib;
  fixture = import ../../examples/metadata-profile.nix;
  evaluate = modules: nixiz.lib.openwrtConfiguration { inherit pkgs modules; };
  base = {
    services.openwrt = {
      enable = true;
      deviceModules = [ fixture ];
      defaults.profiles.metadata.enable = true;
      devices.alpha = {
        profiles.metadata.label = "first";
      };
      devices.beta = {
        profiles.metadata.label = "second";
      };
      fleetModules = [
        ({ fleet, lib, ... }: {
          devices = lib.mapAttrs (_: _: { pkgs, ... }: {
            files."/etc/example-peers.json" = {
              format = "json";
              value = lib.mapAttrs (_: node: node.profiles.metadata.label) fleet;
            };
            files."/etc/example-generated".source = pkgs.writeText "example-generated" "fixture";
          }) fleet;
        })
      ];
    };
  };
  manifest = (evaluate [ base ]).config.services.openwrt.manifest;
  disabled = evaluate [ { services.openwrt.enable = false; } ];
  noProfile =
    (evaluate [
      {
        services.openwrt = {
          enable = true;
          devices.router = {
            target_host = "2001:db8::1";
            files."/etc/example.conf".text = "example\n";
          };
        };
      }
    ]).config.services.openwrt.manifest;
  conflicting = evaluate [
    base
    {
      services.openwrt.devices.alpha.resourceSets.metadata.files."/etc/example-label".text = "conflict";
    }
  ];
  misspelled = evaluate [
    base
    { services.openwrt.devices.alpha.profiles.metadata.lable = "typo"; }
  ];
  assertionFailure = evaluate [
    base
    {
      services.openwrt.devices.alpha.assertions = [
        {
          assertion = false;
          message = "fixture failure";
        }
      ];
    }
  ];
  unknownFleet = evaluate [
    base
    { services.openwrt.fleetModules = [ { devices.absent.files."/etc/example".text = "bad"; } ]; }
  ];
  succeeds = value: (builtins.tryEval (builtins.deepSeq value true)).success;
  nixos =
    modules:
    nixiz.inputs.nixpkgs.lib.nixosSystem {
      system = pkgs.stdenv.hostPlatform.system;
      modules = [
        nixiz.nixosModules.default
        { system.stateVersion = "26.05"; }
      ]
      ++ modules;
    };
  plainNixos = nixos [ ];
  offNixos = nixos [ { services.openwrt.enable = false; } ];
  onNixos = nixos [ base ];
  names = system: map (package: package.name) system.config.environment.systemPackages;
  overridden =
    (evaluate [
      base
      {
        services.openwrt.devices.alpha.resourceSets.metadata.files."/etc/example-label".text =
          lib.mkForce "override";
      }
    ]).config.services.openwrt.manifest;
  noMetadata =
    (evaluate [
      base
      {
        services.openwrt.devices.alpha.profiles.metadata.enable = false;
      }
    ]).config.services.openwrt.manifest;
  removedDevice =
    (evaluate [
      base
      { services.openwrt.devices.beta.enable = false; }
    ]).config.services.openwrt.manifest;
  unknownResource = evaluate [
    base
    { services.openwrt.fleetModules = [ { devices.alpha.unknown = true; } ]; }
  ];
  checked =
    assert manifest.devices.alpha.resourceSets.metadata.files."/etc/example-label".text == "first";
    assert manifest.devices.beta.resourceSets.metadata.files."/etc/example-label".text == "second";
    assert
      manifest.devices.alpha.files."/etc/example-peers.json".value == {
        alpha = "first";
        beta = "second";
      };
    assert !(manifest.devices.alpha ? profiles);
    assert lib.hasPrefix "/nix/store/" manifest.devices.alpha.files."/etc/example-generated".source;
    assert builtins.attrNames removedDevice.devices == [ "alpha" ];
    assert removedDevice.devices.alpha.files."/etc/example-peers.json".value == { alpha = "first"; };
    assert noProfile.devices.router.target_host == "2001:db8::1";
    assert noProfile.devices.router.identity == { };
    assert noProfile.devices.router.users.root.passwordHash == null;
    assert disabled.config.system.build == { };
    assert disabled.config.services.openwrt.manifest.devices == { };
    assert !succeeds conflicting.config.services.openwrt.manifest;
    assert !succeeds misspelled.config.services.openwrt.manifest;
    assert !succeeds assertionFailure.config.services.openwrt.manifest;
    assert !succeeds unknownFleet.config.services.openwrt.manifest;
    assert !succeeds unknownResource.config.services.openwrt.manifest;
    assert overridden.devices.alpha.resourceSets.metadata.files."/etc/example-label".text == "override";
    assert !(noMetadata.devices.alpha.resourceSets ? metadata);
    assert names offNixos == names plainNixos;
    assert
      builtins.attrNames offNixos.config.systemd.services
      == builtins.attrNames plainNixos.config.systemd.services;
    assert offNixos.config.networking.interfaces == plainNixos.config.networking.interfaces;
    assert !(offNixos.config.system.build ? openwrt-manifest);
    assert onNixos.config.services.openwrt.manifest == manifest;
    assert builtins.elem "openwrt-rebuild" (names onNixos);
    assert builtins.elem "openwrt-deploy" (names onNixos);
    true;
in
assert checked;
pkgs.runCommand "nixiz-module-tests" { } ''
  touch "$out"
''
