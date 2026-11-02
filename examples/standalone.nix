# SPDX-License-Identifier: GPL-2.0-only
# Pass this module to wrtix.lib.openwrtConfiguration { inherit pkgs; modules = [ ... ]; }.
{
  services.openwrt = {
    enable = true;
    deviceModules = [ ./metadata-profile.nix ];
    defaults.profiles.metadata.enable = true;
    devices.router = {
      target_host = "router.example.invalid";
      profiles.metadata.label = "Example router";
    };
  };
}
