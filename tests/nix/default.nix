# SPDX-License-Identifier: GPL-2.0-only
{ pkgs, wrtix }:
let
  lib = pkgs.lib;
  fixture = import ../../examples/metadata-profile.nix;
  evaluate = modules: wrtix.lib.openwrtConfiguration { inherit pkgs modules; };
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
  quickStart = (import ../../templates/minimal/flake.nix).outputs { inherit wrtix; };
  quickStartDevice =
    quickStart.openwrtConfigurations.home.config.services.openwrt.manifest.devices.router;
  apExample =
    (evaluate [
      (import ../../templates/ap/ap.nix {
        lib = wrtix.lib;
        targetHost = "root@router.example";
        ssid = "Example Wi-Fi";
        ports = [
          "lan1"
          "lan2"
        ];
        phy = "phy0";
        country = "GB";
        passwordFile = pkgs.writeText "encrypted-example.yaml" "synthetic ciphertext fixture\n";
      })
    ]).config.services.openwrt.manifest.devices.router;
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
    wrtix.inputs.nixpkgs.lib.nixosSystem {
      system = pkgs.stdenv.hostPlatform.system;
      modules = [
        wrtix.nixosModules.default
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
    let
      shorthand = wrtix.lib.uci {
        network.interface.lan = {
          proto = "dhcp";
          device = "br-lan";
        };
        dhcp.dnsmasq = [ { domainneeded = true; } ];
      };
      providerManifest =
        (evaluate [
          {
            services.openwrt = {
              enable = true;
              secretProviders.fixture.executable = pkgs.writeShellScript "fixture-secret" "exit 1";
              devices.router.uci = wrtix.lib.uci {
                example.section.named.value.secretRef = {
                  provider = "fixture";
                  key = "test";
                };
              };
            };
          }
        ]).config.services.openwrt.manifest;
    in
    assert shorthand.network.lan.type == "interface";
    assert shorthand.network.lan.options.proto == "dhcp";
    assert shorthand.dhcp.__anonymous_dnsmasq_0.anonymous;
    assert providerManifest.devices.router.uci.example.named.options.value.secretRef.key == "test";
    assert lib.hasPrefix "/nix/store/" providerManifest.secretProviders.fixture.executable;
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
    assert quickStartDevice.files."/etc/wrtix-example".text == "Managed by wrtix\n";
    assert quickStartDevice.checks == { };
    assert quickStartDevice.identity == { };
    assert quickStartDevice.uci == { };
    assert quickStartDevice.users.root.passwordHash == null;
    assert quickStartDevice.users.root.authorizedKeys == null;
    assert apExample.checks == { };
    assert apExample.users.root.authorizedKeys == null;
    assert apExample.users.root.passwordHash == null;
    assert apExample.uci.network.lan.options.proto == "dhcp";
    assert !apExample.services.dnsmasq.enable;
    assert !apExample.services.odhcpd.enable;
    assert
      apExample.uci.network.bridge.options.ports == [
        "lan1"
        "lan2"
      ];
    assert apExample.uci.network.loopback.options.device == "lo";
    assert apExample.uci.wireless.radio0.options.phy == "phy0";
    assert apExample.uci.wireless.ap.options.network == "lan";
    assert apExample.uci.wireless.ap.options.key.sops.key == "wifi_password";
    assert
      apExample.services.network.reloadOn.uci == [
        "network"
        "wireless"
      ];
    assert
      quickStart.apps.x86_64-linux.default.program
      == "${quickStart.openwrtConfigurations.home.config.system.build.openwrt-rebuild}/bin/openwrt-rebuild";
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
pkgs.runCommand "wrtix-module-tests" { } ''
  touch "$out"
''
