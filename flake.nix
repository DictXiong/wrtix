{
  description = "wrtix: declarative OpenWrt configuration and transactional deployment";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      eachSystem = nixpkgs.lib.genAttrs systems;
    in
    {
      nixosModules.default = import ./nix/nixos.nix;
      lib.uci = import ./nix/uci.nix { lib = nixpkgs.lib; };
      templates.default = self.templates.ap;
      templates.ap = {
        path = ./templates/ap;
        description = "A single-radio wired access point using plain UCI resources";
      };
      templates.minimal = {
        path = ./templates/minimal;
        description = "Manage one OpenWrt device without a custom profile or validator";
      };
      lib.openwrtConfiguration =
        {
          pkgs,
          modules,
          specialArgs ? { },
        }:
        nixpkgs.lib.evalModules {
          specialArgs = specialArgs // {
            inherit pkgs;
          };
          modules = [ ./nix/standalone.nix ] ++ modules;
        };
      packages = eachSystem (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          wrtix = pkgs.callPackage ./nix/package.nix { };
          default = self.packages.${system}.wrtix;
        }
      );
      apps = eachSystem (system: {
        default = self.apps.${system}.openwrt-rebuild;
        openwrt-rebuild = {
          type = "app";
          meta.description = "Build and deploy declarative OpenWrt configurations";
          program = "${self.packages.${system}.wrtix}/bin/openwrt-rebuild";
        };
        openwrt-deploy = {
          type = "app";
          meta.description = "Manage OpenWrt configuration transactions";
          program = "${self.packages.${system}.wrtix}/bin/openwrt-deploy";
        };
      });
      checks = eachSystem (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          modules = import ./tests/nix/default.nix {
            inherit pkgs;
            wrtix = self;
          };
          controller =
            pkgs.runCommand "wrtix-controller-tests"
              {
                nativeBuildInputs = with pkgs; [
                  self.packages.${system}.wrtix.testPythonEnvironment
                  sops
                  ssh-to-age
                  ssh-to-pgp
                  gnupg
                  openssh
                ];
              }
              ''
                export PYTHONDONTWRITEBYTECODE=1
                export WRTIX_AGENT_DATA=${self}/agent
                python3 ${self}/tests/test_controller.py
                python3 ${self}/tests/test_controller_inherited.py
                python3 ${self}/tests/test_workflows.py
                touch "$out"
              '';
          target =
            pkgs.runCommand "wrtix-target-tests"
              {
                nativeBuildInputs = with pkgs; [
                  self.packages.${system}.wrtix.testPythonEnvironment
                  jq
                  util-linux
                  coreutils
                  gnused
                  gawk
                  gnugrep
                  gnutar
                  findutils
                  bash
                ];
              }
              ''
                export PYTHONDONTWRITEBYTECODE=1
                python3 ${self}/tests/test_target.py
                python3 ${self}/tests/test_controller_agent.py
                python3 ${self}/tests/test_diff.py
                touch "$out"
              '';
          backup =
            pkgs.runCommand "wrtix-package-backup-tests"
              {
                nativeBuildInputs = with pkgs; [
                  python3
                  jq
                  util-linux
                  gawk
                  coreutils
                  findutils
                  gnugrep
                  bash
                ];
              }
              ''
                export PYTHONDONTWRITEBYTECODE=1
                python3 ${self}/tests/test_package_backup.py
                touch "$out"
              '';
          package = self.packages.${system}.wrtix;
        }
      );
      formatter = eachSystem (system: nixpkgs.legacyPackages.${system}.nixfmt);
    };
}
