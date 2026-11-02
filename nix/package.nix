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
  pname = "wrtix";
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
    mkdir -p "$out/lib/wrtix" "$out/bin" "$out/share/wrtix"
    cp -r src/wrtix/. "$out/lib/wrtix/"
    cp -r agent "$out/share/wrtix/"
    for entry in openwrt-deploy=openwrt.py openwrt-rebuild=rebuild.py; do
      makeWrapper ${python}/bin/python3 "$out/bin/''${entry%%=*}" \
        --add-flags "$out/lib/wrtix/''${entry#*=}" \
        --set WRTIX_AGENT_DATA "$out/share/wrtix/agent" \
        --prefix PYTHONPATH : "$out/lib/wrtix" \
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
    test -f "$out/share/wrtix/agent/contract.json"
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
