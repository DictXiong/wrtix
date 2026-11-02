#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# This file was generated entirely by AI.
# It has not been reviewed line by line by a human.
# Source-only sysupgrade backup hook. Never changes live configuration.
nixiz_backup_fail() {
 printf 'nixiz: %s; refusing incomplete configuration backup\n' "$1" >&2
 exit 1
}
nixiz_backup_path() {
 local rel=$1 current= part
 [ "${#rel}" -le 4096 ] || return 1
 case "$rel" in ''|/*|*/|*//*|*[!A-Za-z0-9_./+-]*) return 1;; esac
 case "/$rel/" in */../*|*/./*) return 1;; esac
 case "$rel" in
  etc/nixiz/identity.json|etc/nixiz/managed.json|etc/nixiz/root-password-hash) :;;
  etc/nixiz|etc/nixiz/*|etc/shadow|etc/passwd|etc/group|etc/gshadow|etc/resolv.conf|etc/ssh|etc/ssh/*|etc/dropbear/dropbear_*|etc/rc.d|etc/rc.d/*) return 1;;
  etc/init.d/nixiz-managed-*|usr/libexec/nixiz-managed/*) :;;
  etc/init.d/*) return 1;;
  etc/*) :;;
  *) return 1;;
 esac
 while [ -n "$rel" ]; do
  part=${rel%%/*}; current=$current/$part
  [ ! -L "$current" ] || return 1
  case "$rel" in */*) rel=${rel#*/};; *) rel=;; esac
 done
 [ -f "$current" ] && [ "$(ls -nd "$current" | awk '{print $3}')" = 0 ]
}
nixiz_add_managed_conffiles() (
 local destination=$1 ledger=/etc/nixiz/managed.json temporary name status
 umask 077
 [ ! -L /etc/nixiz/transaction.lock ] || nixiz_backup_fail unsafe_lock
 exec 8> /etc/nixiz/transaction.lock
 flock -x 8 || nixiz_backup_fail lock_failed
 [ ! -e /etc/nixiz/package-operation ] || nixiz_backup_fail package_operation_pending
 status=$(/usr/libexec/nixiz/agent status) || nixiz_backup_fail agent_status_unavailable
 printf '%s' "$status" | jq -e '.state=="absent" or (.state=="confirmed" and .reload_complete==true) or .state=="rolled-back"' >/dev/null || nixiz_backup_fail transaction_pending
 [ ! -e /etc/nixiz/transactions/state/restore-pending ] &&
 [ ! -e /etc/nixiz/transactions/state/rollback-reboot ] &&
 [ ! -e /etc/nixiz/transactions/state/rollback-incomplete ] || nixiz_backup_fail restoration_pending
 if [ ! -e "$ledger" ] && [ ! -L "$ledger" ]; then return 0; fi
 [ -f "$destination" ] && [ ! -L "$destination" ] || nixiz_backup_fail invalid_backup_list
 nixiz_backup_path etc/nixiz/managed.json || nixiz_backup_fail unsafe_inventory
 [ "$(ls -nd "$ledger" | awk '{print $1}')" = '-rw-------' ] || nixiz_backup_fail unsafe_inventory_mode
 jq -e '.schema_version==1 and .kind=="nixiz-owned-resources" and
  (.files|type=="array" and length<=128 and all(.[];type=="string") and length==(unique|length))' "$ledger" >/dev/null || nixiz_backup_fail invalid_inventory
 temporary=$(mktemp -d) || nixiz_backup_fail workspace_failed
 : > "$temporary/files"
 jq -r '.files[]' "$ledger" > "$temporary/names" || { rm -rf "$temporary"; nixiz_backup_fail invalid_inventory; }
 while IFS= read -r name; do
  nixiz_backup_path "$name" || { rm -rf "$temporary"; nixiz_backup_fail unsafe_managed_file; }
  printf '/%s\n' "$name" >> "$temporary/files" || nixiz_backup_fail write_failed
 done < "$temporary/names"
 for name in etc/nixiz/managed.json etc/nixiz/identity.json etc/nixiz/root-password-hash; do
  if [ -e "/$name" ] || [ -L "/$name" ]; then
   nixiz_backup_path "$name" || { rm -rf "$temporary"; nixiz_backup_fail unsafe_state_file; }
   printf '/%s\n' "$name" >> "$temporary/files" || nixiz_backup_fail write_failed
  fi
 done
 while IFS= read -r name; do
  if ! grep -Fxq -- "$name" "$destination" && ! grep -Fxq -- "${name#/}" "$destination"; then
   printf '%s\n' "$name" >> "$destination" || nixiz_backup_fail write_failed
  fi
 done < "$temporary/files"
 rm -rf "$temporary"
)
[ -z "${sysupgrade_init_conffiles:-}" ] || append sysupgrade_init_conffiles nixiz_add_managed_conffiles
