{
  inputs.wrtix.url = "github:DictXiong/wrtix";

  outputs =
    { wrtix, ... }:
    let
      system = "x86_64-linux"; # Use aarch64-linux on an ARM controller.
      home = wrtix.lib.openwrtConfiguration {
        pkgs = wrtix.inputs.nixpkgs.legacyPackages.${system};
        modules = [
          {
            services.openwrt = {
              enable = true;
              devices.router = {
                target_host = "root@router.example";
                files."/etc/wrtix-example".text = "Managed by wrtix\n";
              };
            };
          }
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
