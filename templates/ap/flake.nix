# This file was generated entirely by AI.
# It has not been reviewed line by line by a human.
{
  inputs.nixiz.url = "github:DictXiong/nixiz";
  outputs =
    { nixiz, ... }:
    let
      system = "x86_64-linux"; # Use aarch64-linux on an ARM controller.
      home = nixiz.lib.openwrtConfiguration {
        pkgs = nixiz.inputs.nixpkgs.legacyPackages.${system};
        modules = [
          (import ./ap.nix {
            targetHost = "root@router.example";
            ssid = "Example Wi-Fi";
            passwordFile = ./secrets.yaml;
            ports = [
              "lan1"
              "lan2"
            ]; # Include the LAN uplink/maintenance ports.
            phy = "phy0"; # A 2.4 GHz mac80211 PHY on this device.
            country = "GB"; # Set your actual regulatory country.
          })
        ];
      };
    in
    {
      openwrtConfigurations.home = home;
      apps.${system}.default = {
        type = "app";
        program = "${home.config.system.build.openwrt-rebuild}/bin/openwrt-rebuild";
      };
    };
}
