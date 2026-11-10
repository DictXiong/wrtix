# SPDX-License-Identifier: GPL-2.0-only
{ lib }:
settings:
lib.mapAttrs (
  package: groups:
  lib.foldlAttrs (
    sections: type: entries:
    let
      identifier =
        if builtins.match "[A-Za-z0-9_]+" type != null then type else builtins.hashString "sha256" type;
      named =
        if builtins.isList entries then
          lib.listToAttrs (
            lib.imap0 (index: options: {
              name = "__anonymous_${identifier}_${toString index}";
              value = {
                inherit type options;
                anonymous = true;
                order = index;
              };
            }) entries
          )
        else
          lib.mapAttrs (_: options: { inherit type options; }) entries;
      collisions = lib.intersectLists (builtins.attrNames sections) (builtins.attrNames named);
    in
    if collisions != [ ] then
      throw "Duplicate UCI section names in ${package}: ${lib.concatStringsSep ", " collisions}"
    else
      sections // named
  ) { } groups
) settings
