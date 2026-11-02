{
  inputs.wrtix.url = "github:DictXiong/wrtix";
  outputs =
    { wrtix, ... }:
    let
      system = "x86_64-linux"; # Use aarch64-linux on an ARM controller.
      home = wrtix.lib.openwrtConfiguration {
        pkgs = wrtix.inputs.nixpkgs.legacyPackages.${system};
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
