# SPDX-License-Identifier: GPL-2.0-only
{ config, lib, ... }:
let
  cfg = config.services.openwrt;
in
{
  imports = [ ./core.nix ];
  config = lib.mkIf cfg.enable {
    environment.systemPackages = [
      config.system.build.openwrt-deploy
      config.system.build.openwrt-rebuild
    ];
    services.openwrt.decryption = {
      keyFile = lib.mkDefault (
        let
          source = config.sops.age.keyFile or null;
        in
        if source == null then null else toString source
      );
      sshKeyFiles = lib.mkDefault (
        if cfg.decryption.sshKeyFile != null then [ ] else map toString (config.sops.age.sshKeyPaths or [ ])
      );
      gnupgHome = lib.mkDefault (config.sops.gnupg.home or null);
      gnupgSshKeyFiles = lib.mkDefault (
        if cfg.decryption.gnupgHome != null then
          [ ]
        else
          map toString (config.sops.gnupg.sshKeyPaths or [ ])
      );
    };
  };
}
