#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Source-only sysupgrade backup hook. Never changes live configuration.
wrtix_backup_fail() {
 printf 'wrtix: %s; refusing incomplete configuration backup\n' "$1" >&2
 exit 1
}
wrtix_backup_path() {
 local rel=$1 current= part
 [ "${#rel}" -le 4096 ] || return 1
 case "$rel" in ''|/*|*/|*//*|*[!A-Za-z0-9_./+-]*) return 1;; esac
 case "/$rel/" in */../*|*/./*) return 1;; esac
 case "$rel" in
  etc/wrtix/identity.json|etc/wrtix/managed.json|etc/wrtix/root-password-hash) :;;
  etc/wrtix|etc/wrtix/*|etc/shadow|etc/passwd|etc/group|etc/gshadow|etc/resolv.conf|etc/ssh|etc/ssh/*|etc/dropbear/dropbear_*|etc/rc.d|etc/rc.d/*) return 1;;
  etc/init.d/wrtix-managed-*|usr/libexec/wrtix-managed/*) :;;
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
wrtix_add_managed_conffiles() (
 local destination=$1 ledger=/etc/wrtix/managed.json temporary name status
 umask 077
 [ ! -L /etc/wrtix/transaction.lock ] || wrtix_backup_fail unsafe_lock
 exec 8> /etc/wrtix/transaction.lock
 flock -x 8 || wrtix_backup_fail lock_failed
 [ ! -e /etc/wrtix/package-operation ] || wrtix_backup_fail package_operation_pending
 status=$(/usr/libexec/wrtix/agent status) || wrtix_backup_fail agent_status_unavailable
 printf '%s' "$status" | jq -e '.state=="absent" or (.state=="confirmed" and .reload_complete==true) or .state=="rolled-back"' >/dev/null || wrtix_backup_fail transaction_pending
 [ ! -e /etc/wrtix/transactions/state/restore-pending ] &&
 [ ! -e /etc/wrtix/transactions/state/rollback-reboot ] &&
 [ ! -e /etc/wrtix/transactions/state/rollback-incomplete ] || wrtix_backup_fail restoration_pending
 if [ ! -e "$ledger" ] && [ ! -L "$ledger" ]; then return 0; fi
 [ -f "$destination" ] && [ ! -L "$destination" ] || wrtix_backup_fail invalid_backup_list
 wrtix_backup_path etc/wrtix/managed.json || wrtix_backup_fail unsafe_inventory
 [ "$(ls -nd "$ledger" | awk '{print $1}')" = '-rw-------' ] || wrtix_backup_fail unsafe_inventory_mode
 jq -e '.schema_version==1 and .kind=="wrtix-owned-resources" and
  (.files|type=="array" and length<=128 and all(.[];type=="string") and length==(unique|length))' "$ledger" >/dev/null || wrtix_backup_fail invalid_inventory
 temporary=$(mktemp -d) || wrtix_backup_fail workspace_failed
 : > "$temporary/files"
 jq -r '.files[]' "$ledger" > "$temporary/names" || { rm -rf "$temporary"; wrtix_backup_fail invalid_inventory; }
 while IFS= read -r name; do
  wrtix_backup_path "$name" || { rm -rf "$temporary"; wrtix_backup_fail unsafe_managed_file; }
  printf '/%s\n' "$name" >> "$temporary/files" || wrtix_backup_fail write_failed
 done < "$temporary/names"
 for name in etc/wrtix/managed.json etc/wrtix/identity.json etc/wrtix/root-password-hash; do
  if [ -e "/$name" ] || [ -L "/$name" ]; then
   wrtix_backup_path "$name" || { rm -rf "$temporary"; wrtix_backup_fail unsafe_state_file; }
   printf '/%s\n' "$name" >> "$temporary/files" || wrtix_backup_fail write_failed
  fi
 done
 while IFS= read -r name; do
  if ! grep -Fxq -- "$name" "$destination" && ! grep -Fxq -- "${name#/}" "$destination"; then
   printf '%s\n' "$name" >> "$destination" || wrtix_backup_fail write_failed
  fi
 done < "$temporary/files"
 rm -rf "$temporary"
)
[ -z "${sysupgrade_init_conffiles:-}" ] || append sysupgrade_init_conffiles wrtix_add_managed_conffiles
