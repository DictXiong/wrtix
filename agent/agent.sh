#!/bin/sh
# This file was generated entirely by AI.
# It has not been reviewed line by line by a human.
# Versioned configuration transaction. Package/firmware operations are excluded.
set -eu
umask 077
export LC_ALL=C
SYSROOT=
SELF=$0
TEST_MODE=false
if [ "${1:-}" = --test-root ]; then
 SYSROOT=${2:-}
 case "$SYSROOT" in ''|/|*[!A-Za-z0-9_./+-]*) exit 2;; esac
 [ -d "$SYSROOT" ] && [ ! -L "$SYSROOT" ] && [ -f "$SYSROOT/.openwrt-target-test-root" ] || exit 2
 TEST_MODE=true
 shift 2
else
 export PATH=/usr/sbin:/usr/bin:/sbin:/bin
fi
p() { printf '%s%s' "$SYSROOT" "$1"; }
BASE=$(p /etc/nixiz/transactions)
STATE=$BASE/state
ARCHIVES=$BASE/archives
LEDGER=etc/nixiz/managed.json
GUARD=$(p /etc/init.d/nixiz-agent)
BOOT=$(p /proc/sys/kernel/random/boot_id)
UPTIME=$(p /proc/uptime)
fail() {
 case "${RELOAD_PHASE:-}" in activate|restore) (record_reload_failure "$1") 2>/dev/null || :;; esac
 printf 'openwrt-deploy: %s\n' "$1" >&2; exit 1
}
plain() { [ -f "$1" ] && [ ! -L "$1" ]; }
sha() {
 local output
 output=$(sha256sum "$1") || return 1
 output=${output%% *}
 case "$output" in *[!0-9a-f]*|'') return 1;; esac
 [ "${#output}" = 64 ] || return 1
 printf '%s\n' "$output"
}
boot() { cat "$BOOT"; }
now() { cut -d. -f1 "$UPTIME"; }
record() { printf '%s\n' "$2" > "$STATE/$1.new.$$"; mv "$STATE/$1.new.$$" "$STATE/$1"; }
secure_dir() { [ -d "$1" ] && [ ! -L "$1" ]; }
lock() {
 mkdir -p "$(p /etc/nixiz)"
 [ ! -L "$(p /etc/nixiz/transaction.lock)" ] || fail unsafe_lock
 exec 9> "$(p /etc/nixiz/transaction.lock)"
 flock -x 9
}
unlock() { flock -u 9; }
tool() {
 if $TEST_MODE; then sh "$SELF" --test-root "$SYSROOT" "$@"; else sh "$SELF" "$@"; fi
}
valid_token() { case "$1" in *[!0-9a-f]*|'') return 1;; esac; [ "${#1}" = 32 ]; }
no_system_operation() {
 local path pending
 [ ! -e "$(p /etc/nixiz/package-operation)" ] || fail package_operation_active
 for path in /tmp/sysupgrade /tmp/sysupgrade.tgz /tmp/sysupgrade-trigger; do
  [ ! -e "$(p "$path")" ] || fail firmware_operation_active
 done
 if ! $TEST_MODE && [ -x /sbin/uci ]; then
  pending=$(/sbin/uci -q changes) || fail pending_config_check_failed
  [ -z "$pending" ] || fail pending_uci_changes
 fi
}
valid_service() {
 case "$1" in ''|*[!A-Za-z0-9_.+-]*|[.+-]*|nixiz-agent) return 1;; esac
 [ "${#1}" -le 64 ]
}
allowed() {
 case "$1" in ''|/*|*/|*//*|*[!A-Za-z0-9_./+-]*) return 1;; esac
 case "/$1/" in */../*|*/./*) return 1;; esac
 case "$1" in
 etc/nixiz/identity.json|etc/nixiz/root-password-hash) return 0;;
 etc/shadow|etc/passwd|etc/group|etc/gshadow|etc/resolv.conf|etc/ssh/*|etc/dropbear/dropbear_*|etc/init.d/nixiz-agent|etc/nixiz/*) return 1;;
 etc/init.d/*) case "$1" in etc/init.d/nixiz-managed-*) return 0;; *) return 1;; esac;;
 etc/rc.d/*) return 1;;
 etc/*|usr/libexec/nixiz-managed/*) return 0;;
 *) return 1;;
 esac
}
path_safe() {
 local rel=$1 current=$SYSROOT next
 while [ -n "$rel" ]; do
  next=${rel%%/*}; current=$current/$next
  [ ! -L "$current" ] || return 1
  case "$rel" in */*) rel=${rel#*/};; *) rel=;; esac
 done
}
regular_or_absent() {
 local dest=$(p /$1)
 path_safe "$1" || return 1
 [ ! -e "$dest" ] || plain "$dest"
}
file_mode() {
 local bits listing
 listing=$(ls -nd "$1") || return 1
 bits=$(printf '%s\n' "$listing" | awk '{print substr($1,2)}') || return 1
 case "$bits" in *[!rwx-]*) return 1;; esac
 [ "${#bits}" = 9 ] || return 1
 awk -v p="$bits" 'BEGIN{n=0;for(i=1;i<=9;i++){n*=2;if(substr(p,i,1)!="-")n++}printf "%03o\n",n}'
}
agent_ready() {
 plain "$(p /usr/share/nixiz/contract.json)" && plain "$GUARD" || fail missing_agent_runtime
 [ ! -e "$(p /etc/nixiz/package-operation)" ] || fail package_operation_active
}
agent_identity() {
 local device_id= board_name= contract=$(p /usr/share/nixiz/contract.json) password=true resolver=true
 agent_ready
 jq -e '.schema_version==1 and .profile=="nixiz-v1"' "$contract" >/dev/null || fail incompatible_agent
 if [ -e "$(p /etc/nixiz/identity.json)" ]; then
  regular_or_absent etc/nixiz/identity.json || fail invalid_identity
  [ "$(file_mode "$(p /etc/nixiz/identity.json)")" = 600 ] || fail invalid_identity
  device_id=$(jq -er 'select(.schema_version==1)|.device_id|select(type=="string" and length>0 and length<=64)' "$(p /etc/nixiz/identity.json)") || fail invalid_identity
 fi
 if plain "$(p /tmp/sysinfo/board_name)"; then board_name=$(cat "$(p /tmp/sysinfo/board_name)"); fi
 if plain "$(p /etc/nixiz/root-password-hash)"; then
  [ "$(awk -F: '$1=="root"{print $2}' "$(p /etc/shadow)")" = "$(cat "$(p /etc/nixiz/root-password-hash)")" ] || password=false
 fi
 if plain "$(p /$LEDGER)"; then
  local target
  target=$(jq -r '.resolver_target//empty' "$(p /$LEDGER)") || fail invalid_owned_ledger
  if [ -n "$target" ]; then
   [ -L "$(p /etc/resolv.conf)" ] && [ "$(readlink "$(p /etc/resolv.conf)")" = "$target" ] || resolver=false
  fi
 fi
 jq -cn --arg id "$device_id" --arg board "$board_name" --arg boot "$(boot)" --arg contract "$(sha "$contract")" --arg engine "$(sha "$SELF")" --arg guard "$(sha "$GUARD")" --argjson password "$password" --argjson resolver "$resolver" '{schema_version:1,profile:"nixiz-v1",identity:{device_id:$id,board_name:$board},boot_id:$boot,contract_sha256:$contract,engine_sha256:$engine,guard_sha256:$guard,root_password_synced:$password,resolver_correct:$resolver}'
}
reload_error_code() {
 case "$1" in
 finalize_failed|initialization_access_changed|initialization_access_restore_failed|root_password_sync_failed|invalid_manifest|invalid_service_backup|token_mismatch|orphan_reload_child_would_not_stop|restored_network_not_ready|post_reboot_readback_failed|service_disable_failed|service_enable_failed|service_start_failed|service_stop_failed|service_reload_failed|service_restore_failed|service_state_unavailable|service_readback_failed|service_restore_readback_failed|wireless_reload_failed|invalid_network_restart_state|invalid_reboot_state|invalid_lease|invalid_token|no_transaction|missing_native_runtime|agent_check_failed|unsafe_service_link|service_link_read_failed|service_snapshot_failed|unsafe_install_path|unsafe_lock|unsafe_resolver_type|reload_worker_failed|postrollback_check_failed|preconfirm_check_failed) return 0;;
 *) return 1;;
 esac
}
child_exit_kind() {
 case "$1" in
 124) printf timeout;; 125) printf timeout_error;; 126) printf not_executable;; 127) printf not_found;;
 137) printf killed;; 143) printf terminated;; *) printf exit;;
 esac
}
record_reload_failure() {
 local code=$1 dest=$STATE/$RELOAD_PHASE-error.json kind= service= action= child_exit=null exit_kind=
 reload_error_code "$code" || code=reload_worker_failed
 [ ! -e "$dest" ] && [ ! -L "$dest" ] || return 0
 if [ -n "${CHILD_EXIT:-}" ]; then
  case "$CHILD_EXIT" in *[!0-9]*) return 1;; esac
  [ "$CHILD_EXIT" -le 255 ] || return 1
  child_exit=$CHILD_EXIT; exit_kind=$(child_exit_kind "$CHILD_EXIT")
  kind=${CHILD_KIND:-}; service=${CHILD_SERVICE:-}; action=${CHILD_ACTION:-}
 fi
 jq -cn --arg phase "$RELOAD_PHASE" --arg code "$code" --arg kind "$kind" --arg service "$service" --arg action "$action" --argjson exit_code "$child_exit" --arg exit_kind "$exit_kind" \
  '{phase:$phase,code:$code,kind:($kind|select(length>0))//null,service:($service|select(length>0))//null,action:($action|select(length>0))//null,exit_code:$exit_code,exit_kind:($exit_kind|select(length>0))//null}' > "$dest.new.$$" || return 1
 mv "$dest.new.$$" "$dest"
}
reload_failure() {
 local file=$STATE/$1-error.json code
 if ! plain "$file"; then printf 'null\n'; return; fi
 code=$(jq -er .code "$file" 2>/dev/null) && reload_error_code "$code" && jq -ce --arg phase "$1" '
  select(type=="object" and (keys|sort)==["action","code","exit_code","exit_kind","kind","phase","service"] and .phase==$phase and
   (.kind==null or .kind=="service" or .kind=="native" or .kind=="bootstrap" or .kind=="wireless" or .kind=="worker") and
   (.action==null or .action=="start" or .action=="stop" or .action=="enable" or .action=="disable" or .action=="reload" or .action=="restart" or .action=="finalize" or .action=="sync-root-password") and
   (.service==null or (.service|type=="string" and length>0 and length<=64 and (explode as $c | $c[0]!=43 and $c[0]!=45 and $c[0]!=46 and
    all($c[]; (.>=48 and .<=57) or (.>=65 and .<=90) or (.>=97 and .<=122) or .==95 or .==43 or .==45 or .==46)))) and
   (.exit_code==null or (.exit_code|type=="number" and floor==. and .>=0 and .<=255)) and
   (.exit_kind==null or .exit_kind=="timeout" or .exit_kind=="timeout_error" or .exit_kind=="not_executable" or .exit_kind=="not_found" or .exit_kind=="killed" or .exit_kind=="terminated" or .exit_kind=="exit"))' "$file" 2>/dev/null && return
 printf 'null\n'
}
service_running() {
 if $TEST_MODE; then
  if [ -f "$(p /run/openwrt-test-services/$1.running)" ]; then cat "$(p /run/openwrt-test-services/$1.running)"; else printf 'false\n'; fi
 else
  local response
  response=$(ubus call service list "{\"name\":\"$1\"}" 2>/dev/null) || return 1
  [ -n "$response" ] || return 1
  printf '%s' "$response" | jq -er 'select(type=="object")|[.[]?.instances[]?|.running==true]|any|tostring'
 fi
}
service_exec() {
 local service=$1 action=$2
 valid_service "$service" || return 1
 timeout -s TERM -k 2 30 "$(p /etc/init.d/$service)" "$action" >/dev/null 2>&1
}
service_links() {
 local name=$1 link target
 for link in "$(p /etc/rc.d)"/S[0-9][0-9]"$name" "$(p /etc/rc.d)"/K[0-9][0-9]"$name"; do
  [ -L "$link" ] || continue
  target=$(readlink "$link") || fail service_link_read_failed
  case "$target" in "../init.d/$name"|"/etc/init.d/$name") :;; *) fail unsafe_service_link;; esac
  jq -cn --arg name "${link##*/}" --arg target "$target" '{name:$name,target:$target}' || fail service_snapshot_failed
 done
}
safe_init_deletion() {
 local name enabled=false running links
 case "$1" in etc/init.d/nixiz-managed-*) name=${1#etc/init.d/};; *) return 0;; esac
 valid_service "$name" || fail unsafe_service_name
 if [ -x "$(p /etc/init.d/$name)" ]; then service_exec "$name" enabled && enabled=true; fi
 [ "$enabled" = false ] || fail init_service_must_be_disabled
 running=$(service_running "$name") || fail service_state_unavailable
 [ "$running" = false ] || fail init_service_must_be_stopped
 links=$(service_links "$name") || fail service_snapshot_failed
 [ -z "$links" ] || fail init_service_links_present
}
snapshot_services() {
 local request=$1 name present enabled running links work
 work=$(mktemp -d "$(p /tmp)/openwrt-services.XXXXXX") || fail snapshot_workspace_failed
 jq -r '.services[]' "$request" > "$work/names" || fail service_snapshot_failed
 while IFS= read -r name; do
  valid_service "$name" || fail unsafe_service_name
  present=false; enabled=false; running=false
  if [ -x "$(p /etc/init.d/$name)" ] && [ ! -L "$(p /etc/init.d/$name)" ]; then
   present=true
   service_exec "$name" enabled && enabled=true
   running=$(service_running "$name") || fail service_state_unavailable
   case "$running" in true|false) :;; *) fail service_state_unavailable;; esac
  fi
  service_links "$name" > "$work/links" || fail service_snapshot_failed
  links=$(jq -sc . "$work/links") || fail service_snapshot_failed
  jq -cn --arg name "$name" --argjson present "$present" --argjson enabled "$enabled" --argjson running "$running" --argjson links "$links" \
   '{key:$name,value:{present:$present,enabled:$enabled,running:$running,links:$links}}' || fail service_snapshot_failed
 done < "$work/names" > "$work/rows"
 jq -sc 'from_entries' < "$work/rows" || { rm -rf "$work"; return 1; }
 rm -rf "$work"
}
valid_resolver_target() {
 case "$1" in /*) :;; *) return 1;; esac
 case "$1" in /|*//*|*/../*|*/./*|*/..|*/.) return 1;; esac
 printf '%s\n' "$1" | grep -Eq '^/[A-Za-z0-9_./-]+$'
}
valid_saved_resolver_target() {
 [ -n "$1" ] && [ "${#1}" -le 4096 ] || return 1
 printf '%s\n' "$1" | grep -Eq '^[A-Za-z0-9_./-]+$'
}
resolver_state() {
 local dest=$(p /etc/resolv.conf) target digest mode
 if [ -L "$dest" ]; then
  target=$(readlink "$dest") || return 1
  valid_saved_resolver_target "$target" || fail unsupported_resolver_symlink
  jq -cn --arg target "$target" '{type:"symlink",target:$target}'
 elif plain "$dest"; then
  digest=$(sha "$dest") && mode=$(file_mode "$dest") || return 1
  jq -cn --arg sha "$digest" --arg mode "$mode" '{type:"file",sha256:$sha,mode:$mode}'
 elif [ ! -e "$dest" ]; then printf '{"type":"absent"}\n'
 else fail unsafe_resolver_type; fi
}
request_paths() {
 jq -r '.paths[]' "$1" || return 1
 printf '%s\n' "$LEDGER" || return 1
 if jq -e '.paths|index("etc/nixiz/root-password-hash")!=null' "$1" >/dev/null; then printf '%s\n' etc/shadow; fi
}
check_request() {
 jq -se 'length==1 and (.[0]|type=="object" and (.native_runtime|type=="boolean") and (.paths|type=="array" and length<=128 and all(.[];type=="string" and length>0)) and (.services|type=="array" and length<=128 and all(.[];type=="string")) and (.paths|length==(unique|length)) and (.services|length==(unique|length)))' "$1" >/dev/null 2>&1 || fail invalid_check_request
 local rel paths
 paths=$(jq -r '.paths[]' "$1") || fail invalid_check_request
 printf '%s\n' "$paths" | while IFS= read -r rel; do [ -z "$rel" ] || allowed "$rel" || fail unsafe_path; done
}
snapshot() {
 local request=$1 work contract services resolver profile rel digest mode boot_id ledger=null
 check_request "$request"
 agent_ready
 work=$(mktemp -d "$(p /tmp)/openwrt-check.XXXXXX") || fail snapshot_workspace_failed
 contract=$(agent_identity) || { rm -rf "$work"; return 1; }
 profile=$(jq -r .native_runtime "$request") || fail invalid_check_request
 if [ "$profile" = true ]; then
  agent_ready
 fi
 request_paths "$request" > "$work/raw-paths" || fail file_snapshot_failed
 sort -u "$work/raw-paths" > "$work/paths" || fail file_snapshot_failed
 while IFS= read -r rel; do
  regular_or_absent "$rel" || { rm -rf "$work"; fail unsafe_existing_path; }
  if plain "$(p /$rel)"; then
   digest=$(sha "$(p /$rel)") && mode=$(file_mode "$(p /$rel)") || fail file_snapshot_failed
   jq -cn --arg path "$rel" --arg sha "$digest" --arg mode "$mode" '{key:$path,value:{sha256:$sha,mode:$mode}}' || fail file_snapshot_failed
  else jq -cn --arg path "$rel" '{key:$path,value:{sha256:null,mode:null}}' || fail file_snapshot_failed; fi
 done < "$work/paths" > "$work/files"
 services=$(snapshot_services "$request") || { rm -rf "$work"; return 1; }
 resolver=null
 if [ "$profile" = true ]; then resolver=$(resolver_state) || fail resolver_snapshot_failed; fi
 if plain "$(p /$LEDGER)"; then
  ledger=$(jq -ce 'select(.schema_version==1 and .kind=="nixiz-owned-resources" and (.files|type)=="array")' "$(p /$LEDGER)") || { rm -rf "$work"; fail invalid_owned_ledger; }
 fi
 boot_id=$(boot) || fail boot_identity_unavailable
 jq -sc --arg boot "$boot_id" --argjson contract "$contract" --argjson services "$services" --argjson resolver "$resolver" --argjson ledger "$ledger" \
  '(from_entries) as $f | {schema_version:1,boot_id:$boot,identity:$contract.identity,contract_sha256:($contract.contract_sha256//null),files:($f|map_values(.sha256)),modes:($f|map_values(.mode)),services:$services,resolver_state:$resolver,ledger:$ledger,agent:$contract}' < "$work/files" || { rm -rf "$work"; return 1; }
 rm -rf "$work"
}
terminal() { [ -f "$STATE/confirmed" ] || [ -f "$STATE/rolled-back" ]; }
active() { secure_dir "$STATE" && plain "$STATE/manifest.json" && ! terminal; }
load() {
 secure_dir "$STATE" && plain "$STATE/manifest.json" && plain "$STATE/lease" || fail no_transaction
 TOKEN=$(jq -er .token "$STATE/manifest.json")
 valid_token "$TOKEN" || fail invalid_token
 read -r LEASE_BOOT DEADLINE < "$STATE/lease"
 [ "$LEASE_BOOT" = "$(jq -r .boot_id "$STATE/manifest.json")" ] || fail invalid_lease
 case "$DEADLINE" in ''|*[!0-9]*) fail invalid_lease;; esac
 ORIGIN_BOOT=$LEASE_BOOT
 ORIGIN_DEADLINE=$DEADLINE
 REBOOT_REQUIRED=false; REBOOT_STATE=none; REBOOT_REMAINING=0
 if [ -e "$STATE/reboot-required" ]; then
  plain "$STATE/reboot-required" || fail invalid_reboot_state
  case "$(cat "$STATE/reboot-required")" in true) REBOOT_REQUIRED=true; REBOOT_STATE=required;; false) :;; *) fail invalid_reboot_state;; esac
 fi
 if [ -e "$STATE/reboot.json" ]; then
  plain "$STATE/reboot.json" || fail invalid_reboot_state
  jq -se --arg token "$TOKEN" --arg origin "$ORIGIN_BOOT" --argjson deadline "$ORIGIN_DEADLINE" '
   def uint: type=="number" and floor==. and .>=0;
   def uuid: type=="string" and length==36 and .[8:9]=="-" and .[13:14]=="-" and
    .[18:19]=="-" and .[23:24]=="-" and
    ((.[0:8]+.[9:13]+.[14:18]+.[19:23]+.[24:36])|explode|all(.[];(.>=48 and .<=57)or(.>=97 and .<=102)));
   length==1 and (.[0]|.schema_version==1 and .token==$token and .origin_boot_id==$origin and
    .origin_deadline==$deadline and (.armed_at|uint) and (.remaining_seconds|uint and .>0) and
    .armed_at+.remaining_seconds==$deadline and
    (if .state=="armed" then true else
     (.state=="awaiting-resume" or .state=="resumed") and (.boot_id|uuid) and .boot_id!=$origin and
     (if .state=="resumed" then (.resumed_at|uint) and (.deadline|uint) and
      .deadline>.resumed_at and .deadline-.resumed_at<=.remaining_seconds else true end) end))
  ' "$STATE/reboot.json" >/dev/null 2>&1 || fail invalid_reboot_state
  [ "$REBOOT_REQUIRED" = true ] || fail invalid_reboot_state
  REBOOT_STATE=$(jq -r .state "$STATE/reboot.json")
  REBOOT_REMAINING=$(jq -r .remaining_seconds "$STATE/reboot.json")
  if [ "$REBOOT_STATE" = resumed ]; then
   LEASE_BOOT=$(jq -r .boot_id "$STATE/reboot.json")
   DEADLINE=$(jq -r .deadline "$STATE/reboot.json")
  fi
 fi
}
status() {
 if [ ! -d "$STATE" ]; then printf '{"state":"absent","schema_version":1}\n'; return; fi
 load
 local state=staged complete=false changed activate_error restore_error
 if plain "$STATE/changed-paths.json"; then changed=$(cat "$STATE/changed-paths.json"); else changed=$(jq -c '[.files[].path]' "$STATE/manifest.json"); fi
 [ ! -f "$STATE/applying" ] || state=applying
 [ ! -f "$STATE/applied" ] || state=applied
 [ ! -f "$STATE/reload-error" ] || state=reload_failed
 [ ! -f "$STATE/confirmed" ] || state=confirmed
 [ ! -f "$STATE/rolled-back" ] || state=rolled-back
 [ ! -f "$STATE/restore-pending" ] || state=rollback_pending
 [ ! -f "$STATE/rollback-incomplete" ] || state=rollback_incomplete
 [ ! -f "$STATE/reload-complete" ] || complete=true
 if active && [ "$LEASE_BOOT" != "$(boot)" ]; then complete=false; fi
 if [ -e "$STATE/rollback-reboot" ]; then
  plain "$STATE/rollback-reboot" || fail invalid_reboot_state
  [ -f "$STATE/rollback-incomplete" ] || state=rollback_pending
  complete=false
 fi
 activate_error=$(reload_failure activate); restore_error=$(reload_failure restore)
 jq -cn --argjson activate_error "$activate_error" --argjson restore_error "$restore_error" --arg state "$state" --arg token "$TOKEN" --arg boot "$LEASE_BOOT" --arg origin "$ORIGIN_BOOT" --arg current "$(boot)" --arg reboot_state "$REBOOT_STATE" --argjson required "$REBOOT_REQUIRED" --argjson remaining "$REBOOT_REMAINING" --argjson deadline "$DEADLINE" --argjson now "$(now)" --argjson complete "$complete" --argjson changed "$changed" --slurpfile m "$STATE/manifest.json" \
  '{schema_version:1,state:$state,token:$token,boot_id:$boot,origin_boot_id:$origin,current_boot_id:$current,deadline:$deadline,now:$now,reload_complete:$complete,activation_error:$activate_error,restore_error:$restore_error,device_id:$m[0].device_id,changed_paths:$changed,reboot_required:$required,reboot_state:$reboot_state,reboot_remaining_seconds:$remaining}'
}
valid_manifest() {
 jq -se '
 def hex: type=="string" and length==64 and (explode|all(.[];(.>=48 and .<=57)or(.>=97 and .<=102)));
 length==1 and (.[0] | .schema_version==1 and .kind=="nixiz-transaction" and
 (.token|type=="string" and length==32 and (explode|all(.[];(.>=48 and .<=57)or(.>=97 and .<=102)))) and
 (.boot_id|type=="string" and length==36) and (.engine_sha256|hex) and (.guard_sha256|hex) and (.device_id|type=="string" and length>=1 and length<=64) and
 (.native_runtime|type=="boolean") and (.resolver_target==null or (.native_runtime==true and (.resolver_target|type)=="string")) and (if has("initialize") then (.initialize|type=="boolean") else true end) and
 ((.initialize//false)==false or .native_runtime==true) and (if has("board_names") then (.board_names|type=="array" and all(.[];type=="string")) else true end) and
 (.ttl_seconds|type=="number" and floor==. and .>=60 and .<=7200) and
 (.baseline|type=="object" and (.files|type=="object") and (.modes|type=="object") and (.services|type=="object")) and
 (.files|type=="array" and length<=128 and
  all(.[];type=="object" and (.path|type=="string") and (.delete|type=="boolean") and
   (if .delete then .payload==null and .sha256==null and .size==0 else
    (.payload|type=="string") and (.sha256|hex) and (.size|type=="number" and floor==. and .>=0 and .<=1048576) and
    (.mode=="600" or .mode=="644" or .mode=="700" or .mode=="755") end)) and
  ([.[].path]|length==(unique|length)) and ([.[]|select(.delete==false)|.payload]|length==(unique|length))) and
 (.managed_services|type=="array" and all(.[];type=="string") and length==(unique|length)) and
 (. as $m | all(.managed_services[]; . as $s | $m.services|has($s))) and
 (.services|type=="object" and length<=128 and (keys|all(.[];. != "nixiz-agent")) and all(.[];
   (if has("reload") then (.reload|type)=="boolean" else true end) and
   (if has("oneshot") then (.oneshot|type)=="boolean" else true end) and
   (.enable|type=="boolean") and ((.running|type=="boolean") or .running==null) and (.action=="reload" or .action=="restart") and (.enable or (.running==false)))))
 ' "$1" >/dev/null 2>&1 || fail invalid_manifest
 jq -e '
  (.checks//[])|type=="array" and length<=32 and
  (map(.name)|length==(unique|length)) and all(.[];
   (.name|type=="string" and length>0 and length<=64 and (explode|all(.[];(.>=48 and .<=57)or(.>=65 and .<=90)or(.>=97 and .<=122)or .==95 or .==45))) and
   (.phase=="preflight" or .phase=="preconfirm" or .phase=="postrollback") and
   (.executable|type=="string") and
   (.sha256|type=="string" and length==64 and (explode|all(.[];(.>=48 and .<=57)or(.>=97 and .<=102)))) and
   (.timeoutSeconds|type=="number" and floor==. and .>=1 and .<=120) and (.input|type=="object"))
 ' "$1" >/dev/null 2>&1 || fail invalid_checks
 local resolver
 resolver=$(jq -r '.resolver_target//empty' "$1") || fail invalid_manifest
 [ -z "$resolver" ] || valid_resolver_target "$resolver" || fail invalid_manifest
}
manifest_request() { jq '{paths:[.files[].path],services:(.services|keys),native_runtime}' "$1"; }
baseline_matches() {
 local file=$1 work actual
 work=$(mktemp "$(p /tmp)/openwrt-request.XXXXXX")
 manifest_request "$file" > "$work"
 actual=$(snapshot "$work") || { rm -f "$work"; return 1; }
 [ -n "$actual" ] || { rm -f "$work"; return 1; }
 rm -f "$work"
 printf '%s' "$actual" | jq -e --slurpfile m "$file" '
 .boot_id==$m[0].boot_id and .identity==$m[0].baseline.identity and .contract_sha256==$m[0].baseline.contract_sha256 and
 .files==$m[0].baseline.files and .modes==$m[0].baseline.modes and .services==$m[0].baseline.services and .resolver_state==$m[0].baseline.resolver_state
 ' >/dev/null || return 1
 printf '%s' "$actual" | jq -e --slurpfile m "$file" '
 .identity as $i | (($m[0].board_names//[]) as $boards | ($boards|length)==0 or ($boards|index($i.board_name)!=null)) and
 .contract_sha256==$m[0].contract_sha256 and
 (if $m[0].initialize//false then $i.device_id=="" else $i.device_id==$m[0].device_id end)
 ' >/dev/null
}
process_same() {
 local pid=$1 ticks=$2 state actual
 case "$pid:$ticks" in *[!0-9:]*|:*|*:) return 1;; esac
 [ -r "/proc/$pid/stat" ] || return 1
 read -r state actual <<PROCESS
$(awk '{print $3 " " $22}' "/proc/$pid/stat" 2>/dev/null)
PROCESS
 [ "$state" != Z ] && [ "$actual" = "$ticks" ]
}
write_pid() {
 local ticks
 ticks=$(awk '{print $22}' "/proc/$1/stat" 2>/dev/null) || return 1
 case "$ticks" in ''|*[!0-9]*) return 1;; esac
 printf '%s %s %s %s\n' "$1" "$ticks" "$TOKEN" "$(boot)" > "$STATE/$2"
}
recorded_alive() {
 local pid ticks token recorded_boot extra
 plain "$STATE/$1" || return 1
 read -r pid ticks token recorded_boot extra < "$STATE/$1" || return 1
 [ "$token" = "$TOKEN" ] && [ "$recorded_boot" = "$(boot)" ] && [ -z "$extra" ] && process_same "$pid" "$ticks"
}
stop_recorded() {
 local pid ticks token recorded_boot extra tries=0
 plain "$STATE/$1" || return 0
 read -r pid ticks token recorded_boot extra < "$STATE/$1" || return 1
 [ "$token" = "$TOKEN" ] && [ -z "$extra" ] || return 1
 [ "$recorded_boot" = "$(boot)" ] || return 0
 [ "$pid" != "$$" ] || return 0
 process_same "$pid" "$ticks" || return 0
 kill -TERM "$pid" 2>/dev/null || true
 while process_same "$pid" "$ticks"; do
  tries=$((tries+1)); [ "$tries" -lt 5 ] || break; sleep 1
 done
 if process_same "$pid" "$ticks"; then kill -KILL "$pid" 2>/dev/null || true; fi
}
arm_guard() {
 if $TEST_MODE; then
  (trap '' HUP; exec 9>&-; tool watch) >/dev/null 2>&1 </dev/null &
 else
  [ -x "$GUARD" ] && [ ! -L "$GUARD" ] || fail missing_guard
  "$GUARD" enable >/dev/null 2>&1 && "$GUARD" start >/dev/null 2>&1 || fail guard_start_failed
 fi
 local tries=0
 while ! recorded_alive watch.pid; do
  tries=$((tries+1)); [ "$tries" -lt 6 ] || fail guard_not_ready
  sleep 1
 done
}
backup_files() {
 local rel index=0 dest
 mkdir "$STATE/backup"
 request_paths "$STATE/request.json" > "$STATE/backup-paths.raw" || fail backup_paths_failed
 sort -u "$STATE/backup-paths.raw" > "$STATE/backup-paths" || fail backup_paths_failed
 while IFS= read -r rel; do
  dest=$(p /$rel)
  regular_or_absent "$rel" || fail unsafe_backup_path
  if plain "$dest"; then
   cp -p "$dest" "$STATE/backup/$index"
   jq -cn --arg path "$rel" --arg digest "$(sha "$dest")" --arg mode "$(file_mode "$dest")" --argjson index "$index" '{path:$path,present:true,sha256:$digest,mode:$mode,index:$index}'
  else jq -cn --arg path "$rel" --argjson index "$index" '{path:$path,present:false,index:$index}'; fi
  index=$((index+1))
 done < "$STATE/backup-paths" > "$STATE/before.jsonl"
 jq -s . "$STATE/before.jsonl" > "$STATE/before.json"
 jq .services "$STATE/manifest.json" >/dev/null
 jq .services "$STATE/baseline.json" > "$STATE/services.before"
 if [ "$(jq -r .native_runtime "$STATE/manifest.json")" = true ]; then
  resolver_state > "$STATE/resolver.before"
  [ "$(jq -r .type "$STATE/resolver.before")" != file ] || cp -p "$(p /etc/resolv.conf)" "$STATE/resolver.data"
 fi
}
network_mac_signature() {
 local source=$1 directory=$2 section kind address name result has_mac
 mkdir "$directory" "$directory/overrides" "$directory/deltas" || return 1
 if [ -z "$source" ]; then printf '[]\n'; return; fi
 cp "$source" "$directory/network" || return 1
 # Parse the immutable copy with native UCI. Never use the live package or
 # marker: manual UCI edits and deleted MAC options must participate too.
 uci -s -c "$directory" -C "$directory/overrides" -t "$directory/deltas" show network > "$directory/show" 2>/dev/null || return 1
 sed -n 's/^network\.\([^.=]*\)=\(device\|interface\)$/\1/p' "$directory/show" > "$directory/sections" || return 1
 sed 's/=.*//' "$directory/show" > "$directory/keys" || return 1
 : > "$directory/rows"
 while IFS= read -r section; do
  # The strict show distinguishes absent options from a later read failure.
  # get decodes UCI quoting but preserves MAC string case: netifd diffs
  # raw MAC attributes. Failures must not drop MACs silently.
  has_mac=false; address=
  if grep -Fxq "network.$section.macaddr" "$directory/keys"; then has_mac=true
  else result=$?; [ "$result" = 1 ] || return 1; fi
  if [ "$has_mac" = true ]; then
   address=$(uci -s -c "$directory" -C "$directory/overrides" -t "$directory/deltas" -q get "network.$section.macaddr") || return 1
  fi
  kind=$(uci -s -c "$directory" -C "$directory/overrides" -t "$directory/deltas" -q get "network.$section") || return 1
  name=$section
  if [ "$kind" = device ]; then
   if grep -Fxq "network.$section.name" "$directory/keys"; then :
   else result=$?; [ "$result" = 1 ] && [ "$has_mac" = false ] || return 1; continue; fi
   name=$(uci -s -c "$directory" -C "$directory/overrides" -t "$directory/deltas" -q get "network.$section.name") || return 1
   if [ -z "$name" ]; then [ "$has_mac" = false ] || return 1; continue; fi
  fi
  jq -cn --arg kind "$kind" --arg name "$name" --arg address "$address" --argjson has_mac "$has_mac" \
   '{kind:$kind,name:$name,macaddr:(if $has_mac then $address else null end)}' >> "$directory/rows" || return 1
 done < "$directory/sections"
 # Duplicate owners are ambiguous: sorting their conflicting values could
 # hide a section reorder that changes netifd's effective configuration.
 jq -sc 'if (group_by([.kind,.name])|any(.[];length>1 and any(.[];.macaddr!=null))) then error("ambiguous MAC owner") else map(select(.macaddr!=null))|sort_by(.kind,.name) end' "$directory/rows" 2>/dev/null
}
prepare_network_mac_restart() {
 local old= new= index payload digest result enabled changed work=$STATE/network-mac-check.$$
 record network-mac-restart false
 enabled=$(jq -r .native_runtime "$STATE/manifest.json") || fail network_mac_check_failed
 [ "$enabled" = true ] || return 0
 changed=$(jq -r 'index("etc/config/network")!=null' "$STATE/changed-paths.json") || fail network_mac_check_failed
 [ "$changed" = true ] || return 0
 index=$(jq -r '.[]|select(.path=="etc/config/network" and .present)|.index' "$STATE/before.json") || fail network_mac_check_failed
 [ -z "$index" ] || old=$STATE/backup/$index
 payload=$(jq -r '.files[]|select(.path=="etc/config/network" and (.delete|not))|.payload' "$STATE/manifest.json") || fail network_mac_check_failed
 if [ -n "$payload" ]; then
  new=$STATE/candidate/${payload#payload/}
  digest=$(jq -r '.files[]|select(.path=="etc/config/network")|.sha256' "$STATE/manifest.json") || fail network_mac_check_failed
  plain "$new" && [ "$(sha "$new")" = "$digest" ] || fail payload_corrupt
 fi
 # Native parsing is mandatory: legal quoted/escaped UCI option names can
 # spell macaddr without containing that literal substring in the source.
 mkdir "$work" || fail network_mac_check_failed
 network_mac_signature "$old" "$work/old" > "$work/old.json" || fail network_mac_config_invalid
 network_mac_signature "$new" "$work/new" > "$work/new.json" || fail network_mac_config_invalid
 if cmp -s "$work/old.json" "$work/new.json"; then :
 else result=$?; [ "$result" = 1 ] || fail network_mac_check_failed; record network-mac-restart true; fi
 rm -rf "$work"
}
wireless_mac_signature() {
 local source=$1 directory=$2 section kind option value result owner values
 mkdir "$directory" "$directory/overrides" "$directory/deltas" || return 1
 if [ -z "$source" ]; then printf '[]\n'; return; fi
 cp "$source" "$directory/wireless" || return 1
 uci -s -c "$directory" -C "$directory/overrides" -t "$directory/deltas" show wireless > "$directory/show" 2>/dev/null || return 1
 sed -n 's/^wireless\.\([^.=]*\)=\(wifi-device\|wifi-iface\)$/\1/p' "$directory/show" > "$directory/sections" || return 1
 sed 's/=.*//' "$directory/show" > "$directory/keys" || return 1
 : > "$directory/rows"
 while IFS= read -r section; do
  kind=$(uci -s -c "$directory" -C "$directory/overrides" -t "$directory/deltas" -q get "wireless.$section") || return 1
  owner=section:$section
  if [ "$kind" = wifi-iface ]; then
   if grep -Fxq "wireless.$section.ifname" "$directory/keys"; then
    value=$(uci -s -c "$directory" -C "$directory/overrides" -t "$directory/deltas" -q get "wireless.$section.ifname") || return 1
    [ -z "$value" ] || owner=ifname:$value
   else result=$?; [ "$result" = 1 ] || return 1; fi
  fi
  : > "$directory/options"
  for option in macaddr macaddr_base radio_macaddr bssid; do
   if grep -Fxq "wireless.$section.$option" "$directory/keys"; then
    value=$(uci -s -c "$directory" -C "$directory/overrides" -t "$directory/deltas" -q get "wireless.$section.$option") || return 1
    jq -cn --arg key "$option" --arg value "$value" '{key:$key,value:$value}' >> "$directory/options" || return 1
   else result=$?; [ "$result" = 1 ] || return 1; fi
  done
  values=$(jq -sc 'from_entries' "$directory/options") || return 1
  jq -cn --arg kind "$kind" --arg name "$owner" --argjson macs "$values" '{kind:$kind,name:$name,macs:$macs}' >> "$directory/rows" || return 1
 done < "$directory/sections"
 # Include owners with no MAC too: an overriding duplicate without a MAC can
 # change netifd's effective value, and sorting must never conceal that order.
 jq -sc 'if (group_by([.kind,.name])|any(.[];length>1 and any(.[];(.macs|length)>0))) then error("ambiguous wireless MAC owner") else map(select((.macs|length)>0))|sort_by(.kind,.name) end' "$directory/rows" 2>/dev/null
}
prepare_reboot() {
 local changed index payload old= new= work=$STATE/wireless-mac-check.$$
 prepare_network_mac_restart
 record reboot-required false
 [ "$(jq -r .native_runtime "$STATE/manifest.json")" = true ] || return 0
 if network_mac_restart network; then record reboot-required true; fi
 changed=$(jq -r 'index("etc/config/wireless")!=null' "$STATE/changed-paths.json") || fail wireless_mac_check_failed
 [ "$changed" = true ] || return 0
 index=$(jq -r '.[]|select(.path=="etc/config/wireless" and .present)|.index' "$STATE/before.json") || fail wireless_mac_check_failed
 [ -z "$index" ] || old=$STATE/backup/$index
 payload=$(jq -r '.files[]|select(.path=="etc/config/wireless" and (.delete|not))|.payload' "$STATE/manifest.json") || fail wireless_mac_check_failed
 [ -z "$payload" ] || new=$STATE/candidate/${payload#payload/}
 mkdir "$work" || fail wireless_mac_check_failed
 wireless_mac_signature "$old" "$work/old" > "$work/old.json" || fail wireless_mac_config_invalid
 wireless_mac_signature "$new" "$work/new" > "$work/new.json" || fail wireless_mac_config_invalid
 if cmp -s "$work/old.json" "$work/new.json"; then :
 else changed=$?; [ "$changed" = 1 ] || fail wireless_mac_check_failed; record reboot-required true; fi
 rm -rf "$work"
}
network_mac_restart() {
 [ "$1" = network ] || return 1
 [ -e "$STATE/network-mac-restart" ] || return 1
 plain "$STATE/network-mac-restart" || fail invalid_network_restart_state
 case "$(cat "$STATE/network-mac-restart")" in
  true) return 0;; false) return 1;; *) fail invalid_network_restart_state;;
 esac
}
atomic_install() {
 local source=$1 rel=$2 mode=$3 dest=$(p /$2)
 regular_or_absent "$rel" || fail unsafe_install_path
 mkdir -p "${dest%/*}"
 cp "$source" "$dest.openwrt-new.$$"
 chmod "$mode" "$dest.openwrt-new.$$"
 mv "$dest.openwrt-new.$$" "$dest"
}
resolver_install() {
 local dest=$(p /etc/resolv.conf) target
 target=$(jq -r '.resolver_target//empty' "$STATE/manifest.json") || return 1
 [ -n "$target" ] || return 0
 valid_resolver_target "$target" || return 1
 resolver_state >/dev/null
 if [ -L "$dest" ] && [ "$(readlink "$dest")" = "$target" ]; then return; fi
 ln -s -- "$target" "$dest.openwrt-new.$$"
 mv -f "$dest.openwrt-new.$$" "$dest"
}
resolver_matches() {
 local target actual
 target=$(jq -r '.resolver_target//empty' "$STATE/manifest.json") || return 1
 if [ -n "$target" ]; then
  [ -L "$(p /etc/resolv.conf)" ] && [ "$(readlink "$(p /etc/resolv.conf)")" = "$target" ]
 else
  actual=$(resolver_state) || return 1
  printf '%s' "$actual" | jq -e --slurpfile before "$STATE/resolver.before" '.==$before[0]' >/dev/null
 fi
}
resolver_restore() {
 local kind target dest=$(p /etc/resolv.conf)
 [ ! -d "$dest" ] || fail unsafe_resolver_type
 kind=$(jq -r .type "$STATE/resolver.before")
 case "$kind" in
 file)
  [ "$(sha "$STATE/resolver.data")" = "$(jq -r .sha256 "$STATE/resolver.before")" ] || fail resolver_backup_corrupt
  cp -p "$STATE/resolver.data" "$dest.openwrt-restore.$$"; mv -f "$dest.openwrt-restore.$$" "$dest";;
 symlink)
  target=$(jq -r .target "$STATE/resolver.before")
  valid_saved_resolver_target "$target" || fail resolver_backup_corrupt
  ln -s -- "$target" "$dest.openwrt-restore.$$"; mv -f "$dest.openwrt-restore.$$" "$dest";;
 absent) rm -f "$dest";; *) fail resolver_backup_corrupt;; esac
}
restore_service_links() {
 local name link target linkname rows
 jq -r 'keys[]' "$STATE/services.before" > "$STATE/restore-service.names" || fail invalid_service_backup
 while IFS= read -r name; do
  valid_service "$name" || fail invalid_service_backup
  for link in "$(p /etc/rc.d)"/S[0-9][0-9]"$name" "$(p /etc/rc.d)"/K[0-9][0-9]"$name"; do
   [ ! -L "$link" ] || rm "$link"
  done
  rows=$(jq -r --arg n "$name" '.[$n].links[]|[.name,.target]|@tsv' "$STATE/services.before") || fail invalid_service_backup
  printf '%s\n' "$rows" | while IFS="$(printf '\t')" read -r linkname target; do
   [ -n "$linkname" ] || continue
   case "$linkname" in S[0-9][0-9]"$name"|K[0-9][0-9]"$name") :;; *) fail invalid_service_backup;; esac
   case "$target" in "../init.d/$name"|"/etc/init.d/$name") :;; *) fail invalid_service_backup;; esac
   [ ! -e "$(p /etc/rc.d/$linkname)" ] && [ ! -L "$(p /etc/rc.d/$linkname)" ] || fail service_link_conflict
   ln -s "$target" "$(p /etc/rc.d/$linkname)"
  done
 done < "$STATE/restore-service.names"
}
validate_backups() {
 local rel digest index mode name linkname target rows kind
 plain "$STATE/before.json" && plain "$STATE/services.before" || fail missing_backup_metadata
 jq -se 'length==1 and (.[0]|type=="array" and length<=134 and all(.[];
  (.path|type)=="string" and (.present|type)=="boolean" and
  (.index|type=="number" and floor==. and .>=0) and
  (if .present then (.sha256|type=="string" and length==64) and (.mode|type)=="string" else true end)) and
  ([.[].path]|length==(unique|length)) and ([.[].index]|length==(unique|length)))' "$STATE/before.json" >/dev/null || fail invalid_backup_metadata
 jq -r '.[].path' "$STATE/before.json" > "$STATE/before-paths.unsorted" || fail invalid_backup_metadata
 sort "$STATE/before-paths.unsorted" > "$STATE/before-paths.current" || fail invalid_backup_metadata
 cmp -s "$STATE/before-paths.current" "$STATE/backup-paths" || fail invalid_backup_paths
 jq -r '.[]|select(.present)|[.path,.sha256,(.index|tostring),.mode]|@tsv' "$STATE/before.json" > "$STATE/backup-validation.tsv" || fail invalid_backup_metadata
 while IFS="$(printf '\t')" read -r rel digest index mode; do
  path_safe "$rel" && plain "$STATE/backup/$index" && [ "$(sha "$STATE/backup/$index")" = "$digest" ] && [ "$(file_mode "$STATE/backup/$index")" = "$mode" ] || fail corrupt_backup
 done < "$STATE/backup-validation.tsv"
 jq -se 'length==1 and (.[0]|type=="object" and all(.[];(.present|type)=="boolean" and (.enabled|type)=="boolean" and (.running|type)=="boolean" and (.links|type=="array" and all(.[];(.name|type)=="string" and (.target|type)=="string"))))' "$STATE/services.before" >/dev/null || fail invalid_service_backup
 jq -e --slurpfile m "$STATE/manifest.json" '(keys)==($m[0].services|keys)' "$STATE/services.before" >/dev/null || fail invalid_service_backup
 jq -r 'keys[]' "$STATE/services.before" > "$STATE/service-validation.names" || fail invalid_service_backup
 while IFS= read -r name; do
  valid_service "$name" || fail invalid_service_backup
  rows=$(jq -r --arg n "$name" '.[$n].links[]|[.name,.target]|@tsv' "$STATE/services.before") || fail invalid_service_backup
  printf '%s\n' "$rows" | while IFS="$(printf '\t')" read -r linkname target; do
   [ -n "$linkname" ] || continue
   case "$linkname" in S[0-9][0-9]"$name"|K[0-9][0-9]"$name") :;; *) fail invalid_service_backup;; esac
   case "$target" in "../init.d/$name"|"/etc/init.d/$name") :;; *) fail invalid_service_backup;; esac
  done
 done < "$STATE/service-validation.names"
 if [ "$(jq -r .native_runtime "$STATE/manifest.json")" = true ]; then
  plain "$STATE/resolver.before" || fail resolver_backup_corrupt
  kind=$(jq -er .type "$STATE/resolver.before") || fail resolver_backup_corrupt
  case "$kind" in
   file)
    plain "$STATE/resolver.data" && [ "$(sha "$STATE/resolver.data")" = "$(jq -r .sha256 "$STATE/resolver.before")" ] && [ "$(file_mode "$STATE/resolver.data")" = "$(jq -r .mode "$STATE/resolver.before")" ] || fail resolver_backup_corrupt;;
   symlink)
    target=$(jq -er .target "$STATE/resolver.before") || fail resolver_backup_corrupt
    valid_saved_resolver_target "$target" || fail resolver_backup_corrupt;;
   absent) :;; *) fail resolver_backup_corrupt;;
  esac
 fi
}
restore_locked() {
 local reason=$1 rel present index
 stop_recorded reload.pid || fail reload_worker_would_not_stop
 stop_recorded reload.child || fail reload_child_would_not_stop
 if [ ! -f "$STATE/applying" ] && [ ! -f "$STATE/applied" ]; then record rolled-back "$reason"; sync; return; fi
 validate_backups
 jq -r '.[]|[.path,(.present|tostring),(.index|tostring)]|@tsv' "$STATE/before.json" > "$STATE/restore-files.tsv" || fail invalid_backup_metadata
 while IFS="$(printf '\t')" read -r rel present index; do
  path_safe "$rel" || fail unsafe_restore_path
  if [ "$present" = true ]; then
   mkdir -p "$(p /${rel%/*})"
   cp -p "$STATE/backup/$index" "$(p /$rel).openwrt-restore.$$"
   mv "$(p /$rel).openwrt-restore.$$" "$(p /$rel)"
  else rm -f "$(p /$rel)"; fi
 done < "$STATE/restore-files.tsv"
 if [ "$(jq -r .native_runtime "$STATE/manifest.json")" = true ]; then resolver_restore; fi
 restore_service_links
 # Retire candidate completion before publishing rollback to unlocked readers.
 rm -f "$STATE/reload-complete"
 record restore-pending "$reason"
 record rolled-back "$reason"
 # Restoring MACs also needs one baseline boot, including a candidate whose
 # resume expired after network startup. The persistent terminal marker makes
 # that boot finish restoration rather than scheduling another restart.
 if [ "$REBOOT_REQUIRED" = true ]; then
  record rollback-reboot "$(boot) $(now)"
 fi
 sync
}
child_step() {
 CHILD_EXIT=; CHILD_KIND=; CHILD_SERVICE=; CHILD_ACTION=
 case "$2:$3" in
 "$(p /sbin/wifi):reload") CHILD_KIND=wireless; CHILD_ACTION=reload;;
 *)
  local service=${2##*/}
  if [ "$2" = "$(p /etc/init.d/$service)" ] && valid_service "$service"; then
   case "$3" in enable|disable|start|stop|reload|restart) CHILD_KIND=service; CHILD_SERVICE=$service; CHILD_ACTION=$3;; esac
  fi;;
 esac
 (exec 8>&-; exec 9>&-; exec timeout -s TERM -k 2 "$@") >/dev/null 2>&1 &
 local child=$!
 if ! write_pid "$child" reload.child; then rm -f "$STATE/reload.child"; fi
 if wait "$child"; then rm -f "$STATE/reload.child"; return 0
 else CHILD_EXIT=$?; fi
 rm -f "$STATE/reload.child"; return 1
}
desired_services_match() {
 local name present enable running actual actual_enabled links mode=${1:-runtime}
 jq -r '.services|to_entries[]|[.key,(.value.enable|tostring),(.value.running|tostring)]|@tsv' "$STATE/manifest.json" > "$STATE/desired-services.tsv" || return 1
 while IFS="$(printf '\t')" read -r name enable running; do
  if [ ! -x "$(p /etc/init.d/$name)" ]; then
   [ "$enable" = false ] || return 1
   actual=$(service_running "$name") || return 1
   [ "$actual" = false ] || return 1
   links=$(service_links "$name") || return 1
   [ -z "$links" ] || return 1
   continue
  fi
  actual_enabled=false; service_exec "$name" enabled && actual_enabled=true
  [ "$actual_enabled" = "$enable" ] || return 1
  if [ "$mode" != enabled ] && [ "$running" != null ]; then
   actual=$(service_running "$name") || return 1
   [ "$actual" = "$running" ] || return 1
  fi
 done < "$STATE/desired-services.tsv"
}
activation_deferred() {
 [ "$REBOOT_REQUIRED" = true ] && [ "$(boot)" = "$ORIGIN_BOOT" ] || return 1
 case "$REBOOT_STATE" in required|armed) :;; *) return 1;; esac
 plain "$STATE/activation-deferred" && [ "$(cat "$STATE/activation-deferred")" = "$ORIGIN_BOOT" ]
}
wait_desired_services() {
 local tries=0
 while ! desired_services_match; do
  tries=$((tries+1)); [ "$tries" -lt 15 ] || return 1; sleep 1
 done
}
helpers_snapshot() {
 if root_password_managed; then
  local rel=etc/shadow digest mode
  regular_or_absent "$rel" && plain "$(p /$rel)" || return 1
  digest=$(sha "$(p /$rel)") && mode=$(file_mode "$(p /$rel)") || return 1
  jq -cn --arg path "$rel" --arg digest "$digest" --arg mode "$mode" '{path:$path,sha256:$digest,mode:$mode}'
 fi
}
current_matches() {
 local rel deletion digest mode payload ledger_digest expected_digest
 while IFS="$(printf '\t')" read -r rel deletion digest mode payload; do
  regular_or_absent "$rel" || return 1
  if [ "$deletion" = true ]; then [ ! -e "$(p /$rel)" ] || return 1
  else plain "$(p /$rel)" && [ "$(sha "$(p /$rel)")" = "$digest" ] && [ "$(file_mode "$(p /$rel)")" = "$mode" ] || return 1; fi
 done < "$STATE/files.tsv"
 ledger_digest=$(sha "$(p /$LEDGER)") && expected_digest=$(sha "$STATE/ledger.json") || return 1
 [ "$ledger_digest" = "$expected_digest" ] || return 1
 if activation_deferred; then desired_services_match enabled || return 1
 else desired_services_match || return 1; fi
 if [ "$(jq -r .native_runtime "$STATE/manifest.json")" = true ]; then
  resolver_matches || return 1
  root_password_matches || return 1
  if [ -f "$STATE/helpers.after" ]; then
   helpers_snapshot > "$STATE/helpers.current" || return 1
   cmp -s "$STATE/helpers.after" "$STATE/helpers.current" || return 1
  fi
 fi
}
initializing() { jq -e '.initialize==true' "$STATE/manifest.json" >/dev/null; }

initialization_identity_matches() {
 local contract
 contract=$(agent_identity) || return 1
 printf '%s' "$contract" | jq -e --slurpfile m "$STATE/manifest.json" '.identity.device_id==$m[0].device_id and .contract_sha256==$m[0].contract_sha256' >/dev/null
}

# The original boot lease and backup never change. Only this atomic record may
# authorize one candidate boot and bind its replacement uptime lease.
reboot_record() {
 local source=$1
 mv "$source" "$STATE/reboot.json"
 sync
}
reboot_handoff() {
 local current=$(boot) stamp=$(now) expected
 if [ "$REBOOT_STATE" = armed ] && [ "$current" != "$ORIGIN_BOOT" ]; then
  [ "$stamp" -lt 180 ] || return 1
  jq --arg boot "$current" '.state="awaiting-resume"|.boot_id=$boot' "$STATE/reboot.json" > "$STATE/reboot.json.new.$$" || return 1
  reboot_record "$STATE/reboot.json.new.$$"
  rm -f "$STATE/reload-complete"
  load
 fi
 case "$REBOOT_STATE" in
  awaiting-resume)
   expected=$(jq -r .boot_id "$STATE/reboot.json") || return 1
   [ "$current" = "$expected" ] && [ "$stamp" -lt 180 ]; return;;
  *) [ "$LEASE_BOOT" = "$current" ] && [ "$stamp" -lt "$DEADLINE" ];;
 esac
}
request_reboot() {
 lock; load
 [ "${1:-}" = "$TOKEN" ] || fail token_mismatch
 no_system_operation
 active && [ -f "$STATE/applied" ] && [ -f "$STATE/reload-complete" ] && [ ! -f "$STATE/reload-error" ] || fail reload_not_complete
 [ "$REBOOT_REQUIRED" = true ] || fail reboot_not_required
 if [ "$REBOOT_STATE" != required ]; then
  [ "$REBOOT_STATE" = armed ] && [ "$(boot)" = "$ORIGIN_BOOT" ] && [ "$(now)" -lt "$DEADLINE" ] || fail reboot_already_consumed
  unlock; status; return
 fi
 [ "$LEASE_BOOT" = "$(boot)" ] && [ "$(now)" -lt "$DEADLINE" ] && current_matches || fail reboot_readback_mismatch
 local stamp=$(now)
 jq -cn --arg token "$TOKEN" --arg origin "$ORIGIN_BOOT" --argjson deadline "$ORIGIN_DEADLINE" --argjson stamp "$stamp" \
  '{schema_version:1,token:$token,origin_boot_id:$origin,origin_deadline:$deadline,state:"armed",armed_at:$stamp,remaining_seconds:($deadline-$stamp)}' > "$STATE/reboot.json.new.$$"
 reboot_record "$STATE/reboot.json.new.$$"
 record reboot-request "candidate $ORIGIN_BOOT"
 unlock; status
}
resume_reboot() {
 local request boot_id remaining
 request=$(mktemp "$(p /tmp)/openwrt-resume.XXXXXX") || fail resume_workspace_failed
 head -c 4097 > "$request"
 jq -se '
 def uuid: type=="string" and length==36 and .[8:9]=="-" and .[13:14]=="-" and
  .[18:19]=="-" and .[23:24]=="-" and
  ((.[0:8]+.[9:13]+.[14:18]+.[19:23]+.[24:36])|explode|all(.[];(.>=48 and .<=57)or(.>=97 and .<=102)));
 length==1 and (.[0]|type=="object" and (keys|sort)==["boot_id","remaining_seconds"] and
  (.boot_id|uuid) and
  (.remaining_seconds|type=="number" and floor==. and .>0 and .<=7200))' "$request" >/dev/null 2>&1 || { rm -f "$request"; fail invalid_resume_request; }
 boot_id=$(jq -r .boot_id "$request"); remaining=$(jq -r .remaining_seconds "$request"); rm -f "$request"
 lock; load
 [ "${1:-}" = "$TOKEN" ] || fail token_mismatch
 active && [ -f "$STATE/applied" ] && [ "$REBOOT_REQUIRED" = true ] || fail no_reboot_to_resume
 reboot_handoff || fail expired_transaction
 [ "$boot_id" = "$(boot)" ] && [ "$boot_id" != "$ORIGIN_BOOT" ] || fail resume_boot_mismatch
 if [ "$REBOOT_STATE" = resumed ]; then
  # Retries acknowledge the existing deadline; they never renew it.
  [ "$boot_id" = "$LEASE_BOOT" ] || fail resume_boot_mismatch
  unlock; status; return
 fi
 [ "$REBOOT_STATE" = awaiting-resume ] && [ "$remaining" -le "$REBOOT_REMAINING" ] || fail invalid_resume_budget
 jq --argjson stamp "$(now)" --argjson remaining "$remaining" '.state="resumed"|.resumed_at=$stamp|.deadline=($stamp+$remaining)' "$STATE/reboot.json" > "$STATE/reboot.json.new.$$"
 reboot_record "$STATE/reboot.json.new.$$"
 unlock; status
}
post_reboot_readback() {
 if ! $TEST_MODE; then timeout 3 ubus -t 2 list network.interface >/dev/null 2>&1 || return 1; fi
 initialization_identity_matches && current_matches
}
issue_reboot() {
 local kind=$1 expected=$2 stamp=$3 marker=$STATE/$1-reboot-issued
 [ "$(boot)" = "$expected" ] && [ "$(now)" -ge "$((stamp+2))" ] || return 0
 [ ! -f "$marker" ] || return 0
 # A queued restart must never race a firmware upgrade or pending UCI edits.
 (no_system_operation) >/dev/null 2>&1 || return 0
 record "$kind-reboot-issued" "$TOKEN $expected"
 sync
 if ! $TEST_MODE && ! /sbin/reboot >/dev/null 2>&1; then
  record reboot-error "$kind"
  if [ "$kind" = candidate ]; then restore_locked reboot_failed
  else record rollback-incomplete reboot_failed; fi
 fi
}

network_changed() {
 [ "$1" = wireless ] || return 1
 jq -e 'any(.=="etc/config/network" or .=="etc/config/wireless")' "$STATE/changed-paths.json" >/dev/null
}
oneshot_service() {
 jq -e --arg s "$1" '.services[$s].oneshot==true or .services[$s].running==null' "$STATE/manifest.json" >/dev/null
}
restored_services_match() {
 local name present enabled running actual_enabled actual_running
 jq -r 'to_entries[]|[.key,(.value.present|tostring),(.value.enabled|tostring),(.value.running|tostring)]|@tsv' "$STATE/services.before" > "$STATE/restored-services.tsv" || return 1
 while IFS="$(printf '\t')" read -r name present enabled running; do
  [ "$present" = true ] || continue
  actual_enabled=false; service_exec "$name" enabled && actual_enabled=true
  [ "$actual_enabled" = "$enabled" ] || return 1
  actual_running=$(service_running "$name") || return 1
  [ "$actual_running" = "$running" ] || return 1
 done < "$STATE/restored-services.tsv"
}
reload_work() {
 local name enable running action active_now count reload restore_force=false restore_wireless=false restore_boot=false candidate_enable candidate_running rollback_boot rollback_stamp
 TOKEN=$1
 exec 8> "$STATE/reload.lock"
 flock -n 8 || return 0
 lock; load
 [ "$TOKEN" = "$1" ] || fail token_mismatch
 [ ! -f "$STATE/reload-complete" ] || { unlock; return; }
 [ -f "$STATE/applied" ] || [ -f "$STATE/restore-pending" ] || { unlock; return; }
 RELOAD_PHASE=activate
 [ ! -f "$STATE/restore-pending" ] || RELOAD_PHASE=restore
 stop_recorded reload.child || fail orphan_reload_child_would_not_stop
 rm -f "$STATE/reload.child"
 write_pid "$$" reload.pid
 unlock
 if [ -f "$STATE/restore-pending" ]; then
  if [ "$REBOOT_REQUIRED" = true ] && plain "$STATE/rollback-reboot"; then
   read -r rollback_boot rollback_stamp < "$STATE/rollback-reboot"
   if [ "$rollback_boot" = "$(boot)" ]; then
    # Restored MACs and boot links take effect in the protected baseline boot.
    # Do not rebuild the live bridge or reload hardware on the way to it.
    lock; load
    record reboot-request "rollback $(boot)"
    record reload-complete "$(boot) $TOKEN"
    rm -f "$STATE/restore-pending" "$STATE/rollback-incomplete"
    unlock; return
   fi
   restore_boot=true
  fi
  if ! $TEST_MODE && [ "$(jq -r .native_runtime "$STATE/manifest.json")" = true ] && [ "$LEASE_BOOT" != "$(boot)" ]; then
   count=0
   while ! timeout 3 ubus -t 2 list network.interface >/dev/null 2>&1; do
    count=$((count+1)); [ "$count" -lt 30 ] || fail restored_network_not_ready; sleep 2
   done
  fi
  if [ "$restore_boot" = false ]; then
   [ "$LEASE_BOOT" = "$(boot)" ] || restore_force=true
   if [ "$(jq -r .native_runtime "$STATE/manifest.json")" = true ] && network_changed wireless; then restore_wireless=true; fi
  fi
  jq -r 'to_entries[]|[.key,(.value.present|tostring),(.value.running|tostring),(.value.enabled|tostring)]|@tsv' "$STATE/services.before" > "$STATE/restore-services.tsv" || fail invalid_service_backup
  while IFS="$(printf '\t')" read -r name present running enable; do
   [ "$present" = true ] || continue
   active_now=$(service_running "$name") || fail service_state_unavailable
   reload=$(jq -r --arg n "$name" '.services[$n]|if has("reload") then .reload else true end' "$STATE/manifest.json") || fail invalid_manifest
   action=$(jq -r --arg n "$name" '.services[$n].action' "$STATE/manifest.json") || fail invalid_manifest
   if [ "$restore_boot" = true ]; then
    # Boot loaded the baseline configuration. Keep healthy services (including
    # sockets bound to the bridge's current ifindex), but repair missing ones.
    reload=false
   elif [ "$restore_force" = true ] || { [ "$name" = network ] && [ "$restore_wireless" = true ]; }; then reload=true; fi
   if [ "$running" = true ]; then
    if [ "$restore_boot" = false ] && network_mac_restart "$name"; then
     reload=true; action=restart
     [ "$active_now" != false ] || action=start
    fi
    if [ "$active_now" = false ] || [ "$reload" = true ]; then
     if [ "$active_now" = false ] && [ "$action" = reload ]; then action=start; fi
     child_step 30 "$(p /etc/init.d/$name)" "$action" || fail service_restore_failed
    fi
   else
    if [ "$active_now" = true ]; then
     child_step 30 "$(p /etc/init.d/$name)" stop || fail service_restore_failed
     reload=true
    fi
    if [ "$restore_boot" = false ] && oneshot_service "$name"; then
     candidate_enable=$(jq -r --arg n "$name" '.services[$n].enable' "$STATE/manifest.json") || fail invalid_manifest
     candidate_running=$(jq -r --arg n "$name" '.services[$n].running' "$STATE/manifest.json") || fail invalid_manifest
     if [ "$enable" = true ]; then
      if [ "$reload" = true ] || [ "$candidate_enable" != "$enable" ] || [ "$candidate_running" != null ]; then
       child_step 30 "$(p /etc/init.d/$name)" "$action" || fail service_restore_failed
      fi
     elif [ "$candidate_enable" = true ] && [ "$active_now" = false ]; then
      child_step 30 "$(p /etc/init.d/$name)" stop || fail service_restore_failed
     fi
    fi
   fi
  done < "$STATE/restore-services.tsv"
  count=0
  while ! restored_services_match; do
   count=$((count+1)); [ "$count" -lt 15 ] || fail service_restore_readback_failed; sleep 1
  done
  run_checks postrollback "$STATE" || fail postrollback_check_failed
  lock
  if plain "$STATE/rollback-reboot"; then
   read -r rollback_boot rollback_stamp < "$STATE/rollback-reboot"
   if [ "$rollback_boot" != "$(boot)" ]; then
    rm -f "$STATE/rollback-reboot" "$STATE/reboot-error"
    record rollback-reboot-complete "$(boot)"
   else record reboot-request "rollback $rollback_boot"; fi
  fi
  record reload-complete "$(boot) $TOKEN"
  rm -f "$STATE/restore-pending" "$STATE/rollback-incomplete"
  unlock
  return
 fi
 jq -r '.services|to_entries[]|[.key,(.value.enable|tostring),(.value.running|tostring),.value.action,(if .value|has("reload") then .value.reload else true end|tostring)]|@tsv' "$STATE/manifest.json" > "$STATE/activate-services.tsv" || fail invalid_manifest
 if [ "$REBOOT_REQUIRED" = true ] && [ "$REBOOT_STATE" = required ] && [ "$(boot)" = "$ORIGIN_BOOT" ]; then
  # Preserve the initial transport until the controller authorizes the one
  # planned boot. Runtime and management-address checks belong to that boot.
  record activation-deferred "$ORIGIN_BOOT"
  while IFS="$(printf '\t')" read -r name enable running action reload; do
   [ -x "$(p /etc/init.d/$name)" ] || continue
   if [ "$enable" = true ]; then child_step 30 "$(p /etc/init.d/$name)" enable || fail service_enable_failed
   else child_step 30 "$(p /etc/init.d/$name)" disable || fail service_disable_failed; fi
  done < "$STATE/activate-services.tsv"
  helpers_snapshot > "$STATE/helpers.after"
  lock; load
  active && [ "$LEASE_BOOT" = "$(boot)" ] && [ "$(now)" -lt "$DEADLINE" ] && current_matches || { unlock; return 1; }
  record reload-complete "$(boot) $TOKEN"
  unlock; return
 fi
 while IFS="$(printf '\t')" read -r name enable running action reload; do
  [ -x "$(p /etc/init.d/$name)" ] || continue
  if [ "$enable" = false ]; then
   child_step 30 "$(p /etc/init.d/$name)" disable || fail service_disable_failed
   child_step 30 "$(p /etc/init.d/$name)" stop || fail service_stop_failed
  else
   child_step 30 "$(p /etc/init.d/$name)" enable || fail service_enable_failed
   if [ "$running" != false ]; then
    active_now=$(service_running "$name") || fail service_state_unavailable
    if [ "$REBOOT_STATE" = resumed ]; then
     # Boot already loaded the new configuration. Enforce explicit runtime
     # policy without restarting a healthy management/network service again.
     if [ "$running" = true ] && [ "$active_now" = false ]; then
      child_step 30 "$(p /etc/init.d/$name)" start || fail service_start_failed
     fi
     continue
    fi
    if network_mac_restart "$name"; then
     reload=true; action=restart
     [ "$active_now" != false ] || action=start
    fi
    if [ "$running" = true ] && [ "$active_now" = false ] && [ "$action" = reload ]; then
     child_step 30 "$(p /etc/init.d/$name)" start || fail service_start_failed
    elif [ "$reload" = true ] || { [ "$running" = null ] && [ "$(jq -r --arg n "$name" '.[$n].enabled' "$STATE/services.before")" != true ]; }; then
     child_step 30 "$(p /etc/init.d/$name)" "$action" || fail service_reload_failed
    elif [ "$running" = true ] && [ "$active_now" = false ]; then
     child_step 30 "$(p /etc/init.d/$name)" start || fail service_start_failed
    fi
   else child_step 30 "$(p /etc/init.d/$name)" stop || fail service_stop_failed; fi
  fi
 done < "$STATE/activate-services.tsv"
 if [ "$REBOOT_STATE" != resumed ] && [ "$(jq -r .native_runtime "$STATE/manifest.json")" = true ] && network_changed wireless && [ -x "$(p /sbin/wifi)" ]; then child_step 45 "$(p /sbin/wifi)" reload || fail wireless_reload_failed; fi
 wait_desired_services || fail service_readback_failed
 if [ "$REBOOT_STATE" = resumed ]; then
  count=0
  while ! post_reboot_readback; do
   count=$((count+1)); [ "$count" -lt 30 ] || fail post_reboot_readback_failed; sleep 2
  done
 elif [ "$(jq -r .native_runtime "$STATE/manifest.json")" = true ]; then helpers_snapshot > "$STATE/helpers.after"; fi
 lock
 active && [ "$LEASE_BOOT" = "$(boot)" ] && [ "$(now)" -lt "$DEADLINE" ] && current_matches || { unlock; return 1; }
 record reload-complete "$(boot) $TOKEN"
 unlock
}
spawn_reload() {
 recorded_alive reload.pid && return 0
 (trap '' HUP; exec 9>&-;
  if tool reload "$TOKEN"; then :
  else
   code=$?; lock
   CHILD_EXIT=$code; CHILD_KIND=worker; CHILD_SERVICE=; CHILD_ACTION=
   if [ -f "$STATE/restore-pending" ]; then
    RELOAD_PHASE=restore; record_reload_failure reload_worker_failed || :
    record rollback-incomplete service_restore_failed
   elif active; then
    case "$code" in
     137|143)
      count=0; [ ! -f "$STATE/reload-recoveries" ] || count=$(cat "$STATE/reload-recoveries")
      case "$count" in 0|1|2) record reload-recoveries "$((count+1))";; *) RELOAD_PHASE=activate; record_reload_failure reload_worker_failed || :; record reload-error recovery_exhausted;; esac;;
     *) RELOAD_PHASE=activate; record_reload_failure reload_worker_failed || :; record reload-error failed;;
    esac
   fi
   unlock
  fi
 ) >/dev/null 2>&1 </dev/null &
}
watch() {
 trap '' HUP
 load
 [ -z "${1:-}" ] || [ "$1" = "$TOKEN" ] || return 0
 local watching_token=$TOKEN
 write_pid "$$" watch.pid
 while :; do
  lock
  load
  [ "$TOKEN" = "$watching_token" ] || { unlock; return; }
  if [ -f "$STATE/restore-pending" ]; then spawn_reload; unlock; sleep 1; continue; fi
  if terminal; then
   if plain "$STATE/rollback-reboot"; then
    read -r rollback_boot rollback_stamp < "$STATE/rollback-reboot"
    if [ "$rollback_boot" = "$(boot)" ]; then
     issue_reboot rollback "$rollback_boot" "$rollback_stamp"
    else
     record restore-pending planned_rollback_boot
     rm -f "$STATE/reload-complete"
    fi
    unlock; sleep 1; continue
   fi
   unlock; return
  fi
  if ! reboot_handoff; then restore_locked expired_or_reboot
  elif [ -f "$STATE/reload-error" ]; then restore_locked activation_failed
  elif [ "$REBOOT_STATE" = awaiting-resume ]; then post_reboot_readback || true
  elif [ "$REBOOT_STATE" = armed ]; then issue_reboot candidate "$ORIGIN_BOOT" "$(jq -r .armed_at "$STATE/reboot.json")"
  elif [ -f "$STATE/applied" ] && [ ! -f "$STATE/reload-complete" ]; then spawn_reload; fi
  unlock; sleep 1
 done
}
stage() {
 lock
 agent_ready
 no_system_operation
 [ ! -e "$BASE" ] || secure_dir "$BASE" || fail unsafe_runtime_directory
 mkdir -p "$BASE"
 [ ! -e "$ARCHIVES" ] || secure_dir "$ARCHIVES" || fail unsafe_archive_directory
 [ ! -L "$STATE" ] || fail unsafe_state_directory
 mkdir -p "$ARCHIVES"
 local previous_token=
 if [ -d "$STATE" ]; then
  terminal && [ ! -f "$STATE/restore-pending" ] && [ ! -e "$STATE/rollback-reboot" ] || fail transaction_active
  load
  [ ! -e "$ARCHIVES/$TOKEN" ] || fail archive_exists
  previous_token=$TOKEN
 fi
 local incoming=$BASE/incoming.$$ rel deletion digest mode payload expected actual service
 [ ! -e "$incoming" ] && [ ! -L "$incoming" ] || fail incoming_exists
 mkdir "$incoming"
 trap 'rm -rf "$incoming"' EXIT
 head -c 16777217 > "$incoming/bundle.tar"
 [ "$(wc -c < "$incoming/bundle.tar" | tr -d ' ')" -le 16777216 ] || fail bundle_too_large
 tar -tf "$incoming/bundle.tar" > "$incoming/members" 2>/dev/null || fail invalid_archive
 [ "$(sort "$incoming/members" | uniq -d | wc -l | tr -d ' ')" = 0 ] || fail duplicate_archive_member
 [ "$(grep -cx manifest.json "$incoming/members")" = 1 ] || fail missing_manifest
 tar -tvf "$incoming/bundle.tar" > "$incoming/member-details" 2>/dev/null || fail invalid_archive
 while IFS= read -r detail; do case "$detail" in -*) :;; *) fail nonregular_archive_member;; esac; done < "$incoming/member-details"
 tar -xOf "$incoming/bundle.tar" manifest.json > "$incoming/manifest.json" 2>/dev/null || fail invalid_manifest
 valid_manifest "$incoming/manifest.json"
 [ "$(sha "$SELF")" = "$(jq -r .engine_sha256 "$incoming/manifest.json")" ] || fail engine_version_changed
 plain "$GUARD" && [ "$(sha "$GUARD")" = "$(jq -r .guard_sha256 "$incoming/manifest.json")" ] || fail guard_version_changed
 printf 'manifest.json\n' > "$incoming/expected"
 jq -r '.files[]|select(.delete==false)|.payload' "$incoming/manifest.json" >> "$incoming/expected"
 sort "$incoming/expected" > "$incoming/expected.sorted"; sort "$incoming/members" > "$incoming/members.sorted"
 cmp -s "$incoming/expected.sorted" "$incoming/members.sorted" || fail unexpected_archive_member
 jq -e '.files|to_entries|all(.[];.value.delete or .value.payload==("payload/"+(.key|tostring)))' "$incoming/manifest.json" >/dev/null || fail invalid_payload_name
 jq -r '.files[]|[.path,(.delete|tostring),(.sha256//"-"),(.mode//"-"),(.payload//"-")]|@tsv' "$incoming/manifest.json" > "$incoming/files.tsv"
 mkdir "$incoming/candidate"
 while IFS="$(printf '\t')" read -r rel deletion digest mode payload; do
  allowed "$rel" && regular_or_absent "$rel" || fail unsafe_path
  if [ "$deletion" = true ]; then
   plain "$(p /$LEDGER)" && jq -e --arg p "$rel" '.files|index($p)!=null' "$(p /$LEDGER)" >/dev/null || fail deletion_not_owned
   safe_init_deletion "$rel"
  else
   tar -xOf "$incoming/bundle.tar" "$payload" > "$incoming/candidate/${payload#payload/}" 2>/dev/null || fail missing_payload
   [ "$(sha "$incoming/candidate/${payload#payload/}")" = "$digest" ] && [ "$(wc -c < "$incoming/candidate/${payload#payload/}" | tr -d ' ')" = "$(jq -r --arg p "$rel" '.files[]|select(.path==$p)|.size' "$incoming/manifest.json")" ] || fail payload_mismatch
  fi
 done < "$incoming/files.tsv"
 jq -r '.services|keys[]' "$incoming/manifest.json" | while IFS= read -r service; do
  valid_service "$service" || fail unsafe_service_name
  if [ "$(jq -r --arg s "$service" '.services[$s].enable' "$incoming/manifest.json")" = true ]; then
   if ! { [ -x "$(p /etc/init.d/$service)" ] && [ ! -L "$(p /etc/init.d/$service)" ]; }; then
    # A newly declared managed init script is installed before activation.
    jq -e --arg path "etc/init.d/$service" '.files|any(.[]; .path==$path and .delete==false and (.mode=="700" or .mode=="755"))' "$incoming/manifest.json" >/dev/null || fail missing_required_service
   fi
  fi
 done
 validate_identity_payload "$incoming"
 run_checks preflight "$incoming" || fail preflight_check_failed
 baseline_matches "$incoming/manifest.json" || fail baseline_changed
 manifest_request "$incoming/manifest.json" > "$incoming/request.json"
 jq '. as $m | [.files[] | . as $f | select(if .delete then $m.baseline.files[$f.path]!=null else .sha256!=$m.baseline.files[$f.path] or .mode!=$m.baseline.modes[$f.path] end) | .path]' "$incoming/manifest.json" > "$incoming/changed-paths.json"
 jq .baseline "$incoming/manifest.json" > "$incoming/baseline.json"
 jq '{schema_version:1,kind:"nixiz-owned-resources",device_id,files:[.files[]|select(.delete==false)|.path],services:.managed_services,resolver_target}' "$incoming/manifest.json" > "$incoming/ledger.json"
 rm "$incoming/bundle.tar"
 [ -z "$previous_token" ] || mv "$STATE" "$ARCHIVES/$previous_token"
 mv "$incoming" "$STATE"
 trap - EXIT
 load_manifest_token=$(jq -r .token "$STATE/manifest.json")
 printf '%s %s\n' "$(boot)" "$(( $(now) + $(jq -r .ttl_seconds "$STATE/manifest.json") ))" > "$STATE/lease"
 TOKEN=$load_manifest_token
 sync
 unlock
 arm_guard
 status
}
apply() {
 lock; load
 [ "${1:-}" = "$TOKEN" ] || fail token_mismatch
 no_system_operation
 active && [ "$LEASE_BOOT" = "$(boot)" ] && [ "$(now)" -lt "$DEADLINE" ] || fail expired_transaction
 [ ! -f "$STATE/applying" ] && [ ! -f "$STATE/applied" ] || fail already_applied
 recorded_alive watch.pid || fail guard_not_ready
 baseline_matches "$STATE/manifest.json" || fail baseline_changed
 local checked_rel checked_delete checked_digest checked_mode checked_payload
 while IFS="$(printf '\t')" read -r checked_rel checked_delete checked_digest checked_mode checked_payload; do
  [ "$checked_delete" != true ] || safe_init_deletion "$checked_rel"
 done < "$STATE/files.tsv"
 backup_files
 validate_backups
 # Decide before the first managed write, then retain the same decision for
 # rollback even if apply later fails or restored configuration is reordered.
 prepare_reboot
 record applying "$(boot)"
 local rel deletion digest mode payload
 while IFS="$(printf '\t')" read -r rel deletion digest mode payload; do
  if [ "$deletion" = true ]; then rm -f "$(p /$rel)"
  else
   [ "$(sha "$STATE/candidate/${payload#payload/}")" = "$digest" ] || fail payload_corrupt
   if ! { plain "$(p /$rel)" && [ "$(sha "$(p /$rel)")" = "$digest" ] && [ "$(file_mode "$(p /$rel)")" = "$mode" ]; }; then
    atomic_install "$STATE/candidate/${payload#payload/}" "$rel" "$mode"
   fi
  fi
 done < "$STATE/files.tsv"
 atomic_install "$STATE/ledger.json" "$LEDGER" 600
 if [ "$(jq -r .native_runtime "$STATE/manifest.json")" = true ]; then
  resolver_install
 fi
 root_password_apply

 record applied "$(boot)"; sync
 unlock; status
}
root_password_managed() {
 jq -e 'any(.files[]; .path=="etc/nixiz/root-password-hash" and (.delete|not))' "$STATE/manifest.json" >/dev/null
}
root_password_matches() {
 root_password_managed || return 0
 plain "$(p /etc/nixiz/root-password-hash)" && plain "$(p /etc/shadow)" || return 1
 [ "$(awk -F: '$1=="root"{print $2}' "$(p /etc/shadow)")" = "$(cat "$(p /etc/nixiz/root-password-hash)")" ]
}
root_password_apply() {
 root_password_managed || return 0
 root_password_matches && return 0
 local material=$(p /etc/nixiz/root-password-hash) shadow=$(p /etc/shadow) hash
 plain "$material" && plain "$shadow" && [ "$(wc -l < "$material" | tr -d ' ')" = 1 ] || fail invalid_password_material
 IFS= read -r hash < "$material"
 case "$hash" in '$6$'*|'$5$'*|'$y$'*) :;; *) fail invalid_password_material;; esac
 case "$hash" in *:*|*' '*) fail invalid_password_material;; esac
 [ "$(awk -F: '$1=="root"{n++}END{print n+0}' "$shadow")" = 1 ] || fail invalid_shadow
 awk 'BEGIN{FS=OFS=":"} FNR==NR{hash=$0;next}$1=="root"{$2=hash}{print}' "$material" "$shadow" > "$shadow.nixiz-new.$$"
 chmod 600 "$shadow.nixiz-new.$$"; mv "$shadow.nixiz-new.$$" "$shadow"
 root_password_matches || fail root_password_sync_failed
}
validate_identity_payload() {
 local base=$1 payload
 payload=$(jq -er '.files[]|select(.path=="etc/nixiz/identity.json" and .delete==false and .mode=="600")|.payload' "$base/manifest.json") || fail missing_candidate_identity
 jq -se --slurpfile m "$base/manifest.json" 'length==1 and (.[0]|type=="object" and (keys|sort)==["device_id","schema_version"] and .schema_version==1 and .device_id==$m[0].device_id)' "$base/candidate/${payload#payload/}" >/dev/null || fail invalid_candidate_identity
}
run_checks() {
 local phase=$1 base=$2 row name executable payload digest timeout input work source
 work=$(mktemp -d "$(p /tmp)/nixiz-checks.XXXXXX") || return 1
 jq -c --arg phase "$phase" '.checks//[]|.[]|select(.phase==$phase)' "$base/manifest.json" > "$work/rows" || { rm -rf "$work"; return 1; }
 while IFS= read -r row; do
  printf '%s\n' "$row" > "$work/check.json"
  name=$(jq -er .name "$work/check.json") || { rm -rf "$work"; return 1; }
  executable=$(jq -er .executable "$work/check.json") || { rm -rf "$work"; return 1; }
  case "$executable" in /usr/libexec/nixiz-managed/*) :;; *) rm -rf "$work"; return 1;; esac
  allowed "${executable#/}" || { rm -rf "$work"; return 1; }
  payload=$(jq -er --arg p "${executable#/}" '.files[]|select(.path==$p and .delete==false and (.mode=="700" or .mode=="755"))|.payload' "$base/manifest.json") || { rm -rf "$work"; return 1; }
  source="$base/candidate/${payload#payload/}"
  digest=$(jq -er .sha256 "$work/check.json") || { rm -rf "$work"; return 1; }
  plain "$source" && [ "$(sha "$source")" = "$digest" ] || { rm -rf "$work"; return 1; }
  timeout=$(jq -er '.timeoutSeconds|select(type=="number" and floor==. and .>=1 and .<=120)' "$work/check.json") || { rm -rf "$work"; return 1; }
  jq '.input//{}' "$work/check.json" > "$work/input" || { rm -rf "$work"; return 1; }
  cp "$source" "$work/execute" && chmod 700 "$work/execute" || { rm -rf "$work"; return 1; }
  if ! (exec 8>&-; exec 9>&-; timeout -s TERM -k 2 "$timeout" "$work/execute" < "$work/input" > /dev/null 2>&1); then
   rm -rf "$work"; return 1
  fi
 done < "$work/rows"
 rm -rf "$work"
}

verify_installed() (
 local work row executable rel payload index=0 source mode
 work=$(mktemp -d "$(p /tmp)/nixiz-verify.XXXXXX") || fail check_workspace_failed
 trap 'rm -rf "$work"' EXIT
 head -c 1048577 > "$work/request.json"
 [ "$(wc -c < "$work/request.json" | tr -d ' ')" -le 1048576 ] || fail request_too_large
 jq -se 'length==1 and (.[0]|type=="object" and (.checks|type=="array" and length<=32 and all(.[];.phase=="preconfirm")))' "$work/request.json" >/dev/null || fail invalid_checks
 mkdir "$work/candidate"
 : > "$work/files.jsonl"
 jq -c '.checks[]' "$work/request.json" > "$work/rows"
 while IFS= read -r row; do
  executable=$(printf '%s' "$row" | jq -er .executable) || fail invalid_checks
  case "$executable" in /usr/libexec/nixiz-managed/*) :;; *) fail invalid_checks;; esac
  rel=${executable#/}
  allowed "$rel" && regular_or_absent "$rel" && plain "$(p /$rel)" || fail invalid_checks
  plain "$(p /$LEDGER)" && jq -e --arg p "$rel" '.files|index($p)!=null' "$(p /$LEDGER)" >/dev/null || fail check_not_owned
  mode=$(file_mode "$(p /$rel)") || fail invalid_checks
  case "$mode" in 700|755) :;; *) fail invalid_checks;; esac
  if jq -se --arg p "$rel" 'any(.[]; .path==$p)' "$work/files.jsonl" >/dev/null; then continue; fi
  cp "$(p /$rel)" "$work/candidate/$index" || fail check_snapshot_failed
  jq -cn --arg p "$rel" --arg payload "payload/$index" --arg mode "$mode" '{path:$p,payload:$payload,mode:$mode,delete:false}' >> "$work/files.jsonl"
  index=$((index+1))
 done < "$work/rows"
 jq --slurpfile files "$work/files.jsonl" '.+{files:$files}' "$work/request.json" > "$work/manifest.json"
 run_checks preconfirm "$work" || fail preconfirm_check_failed
 printf '{"matches":true}\n'
)

command=${1:-}
case "$command" in
 verify) verify_installed;;
 identity) agent_identity;;
 package-idle) lock; if active || [ -f "$STATE/restore-pending" ] || [ -e "$STATE/rollback-reboot" ]; then fail transaction_active; fi; unlock;;
 check)
  work=$(mktemp "$(p /tmp)/openwrt-check-request.XXXXXX")
  trap 'rm -f "$work"' EXIT
  head -c 262145 > "$work"
  [ "$(wc -c < "$work" | tr -d ' ')" -le 262144 ] || fail request_too_large
  snapshot "$work";;
 stage) stage;;
 apply) apply "${2:-}";;
 status) status;;
 confirm)
  lock; load; [ "${2:-}" = "$TOKEN" ] || fail token_mismatch
  active && [ -f "$STATE/applied" ] && [ -f "$STATE/reload-complete" ] && [ ! -f "$STATE/reload-error" ] || fail reload_not_complete
  [ "$REBOOT_REQUIRED" = false ] || [ "$REBOOT_STATE" = resumed ] || fail reboot_required
  [ "$LEASE_BOOT" = "$(boot)" ] && [ "$(now)" -lt "$DEADLINE" ] && current_matches || fail confirmation_readback_mismatch
  initialization_identity_matches || fail confirmation_identity_mismatch
  run_checks preconfirm "$STATE" || fail preconfirm_check_failed
  [ "$(now)" -lt "$DEADLINE" ] && current_matches || fail confirmation_readback_mismatch
  record confirmed "$(boot)"; sync; unlock
  if ! $TEST_MODE; then "$GUARD" disable >/dev/null 2>&1 || true; fi
  status;;
 rollback)
  lock; load; [ "${2:-}" = "$TOKEN" ] || fail token_mismatch
  [ ! -f "$STATE/confirmed" ] || fail already_confirmed
  if [ ! -f "$STATE/rolled-back" ]; then restore_locked manual; fi
  unlock; status;;
 reboot) request_reboot "${2:-}";;
 resume) resume_reboot "${2:-}";;
 early)
  lock
  if active; then
   load; if ! reboot_handoff; then restore_locked early_expiry_or_reboot; fi
  elif [ -f "$STATE/rolled-back" ] && plain "$STATE/rollback-reboot"; then
   read -r rollback_boot rollback_stamp < "$STATE/rollback-reboot"
   if [ "$rollback_boot" != "$(boot)" ]; then
    record restore-pending planned_rollback_boot
    rm -f "$STATE/reload-complete"
   fi
  fi
  unlock;;
 watch) watch "${2:-}";;
 reload) reload_work "${2:-}";;
 *) fail invalid_command;;
esac
