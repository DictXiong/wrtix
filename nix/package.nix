# This file was generated entirely by AI.
# It has not been reviewed line by line by a human.
# SPDX-License-Identifier: GPL-2.0-only
{
  lib,
  stdenvNoCC,
  makeWrapper,
  python3,
  openssh,
  sshpass,
  sops,
  ssh-to-age,
  ssh-to-pgp,
  gnupg,
  nix,
}:
let
  python = python3.withPackages (ps: [ ps.pyyaml ]);
in
stdenvNoCC.mkDerivation {
  pname = "nixiz";
  version = "0.2.0";
  src = lib.cleanSourceWith {
    src = ../.;
    filter =
      path: type:
      let
        name = baseNameOf path;
      in
      name != ".git" && name != "__pycache__" && name != "evidence" && !(lib.hasSuffix ".pyc" name);
  };
  nativeBuildInputs = [ makeWrapper ];
  dontConfigure = true;
  dontBuild = true;
  dontPatchShebangs = true;
  installPhase = ''
    runHook preInstall
    mkdir -p "$out/lib/nixiz" "$out/bin" "$out/share/nixiz"
    cp -r src/nixiz/. "$out/lib/nixiz/"
    cp -r agent "$out/share/nixiz/"
    for entry in openwrt-deploy=openwrt.py openwrt-rebuild=rebuild.py; do
      makeWrapper ${python}/bin/python3 "$out/bin/''${entry%%=*}" \
        --add-flags "$out/lib/nixiz/''${entry#*=}" \
        --set NIXIZ_AGENT_DATA "$out/share/nixiz/agent" \
        --prefix PYTHONPATH : "$out/lib/nixiz" \
        --prefix PATH : ${
          lib.makeBinPath [
            python
            openssh
            sshpass
            sops
            ssh-to-age
            ssh-to-pgp
            gnupg
            nix
          ]
        }
    done
    runHook postInstall
  '';
  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck
    "$out/bin/openwrt-deploy" --help >/dev/null
    "$out/bin/openwrt-rebuild" --help >/dev/null
    test -f "$out/share/nixiz/agent/contract.json"
    runHook postInstallCheck
  '';
  passthru = {
    pythonEnvironment = python;
    testPythonEnvironment = python3.withPackages (ps: [
      ps.pyyaml
      ps.paramiko
      ps.jsonschema
    ]);
  };
  meta = {
    description = "Declarative OpenWrt configuration and transactional deployment";
    license = lib.licenses.gpl2Only;
    platforms = [
      "x86_64-linux"
      "aarch64-linux"
    ];
    mainProgram = "openwrt-rebuild";
  };
}
