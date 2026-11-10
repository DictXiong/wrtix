#!/usr/bin/env python3
"""Real shell transaction regression tests in isolated synthetic file trees."""
import hashlib, io, json, os, signal, shutil, shlex, stat, subprocess, tempfile, time, tarfile, unittest, uuid
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "agent/agent.sh"
GUARD_SOURCE = ROOT / "agent/guard.init"
HASH = "$6$fixture$" + "a" * 86

class TargetTransaction(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wrtix-target-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.engine_env = dict(os.environ)
        for path in ("tmp", "root", "etc/config", "etc/init.d", "etc/rc.d", "proc/sys/kernel/random", "run/openwrt-test-services", "usr/share/wrtix", "etc/wrtix"):
            (self.root / path).mkdir(parents=True, exist_ok=True)
        self.put(".openwrt-target-test-root", b"synthetic test root\n")
        self.put("proc/sys/kernel/random/boot_id", b"00000000-0000-4000-8000-000000000001\n")
        self.put("proc/uptime", b"100.00 0.00\n")
        self.put("etc/init.d/wrtix-agent", GUARD_SOURCE.read_bytes(), 0o755)
        self.put("usr/share/wrtix/contract.json", (ROOT / "agent/contract.json").read_bytes())
        self.identity = {"schema_version": 1, "device_id": "fixture"}
        self.put("etc/wrtix/identity.json", json.dumps(self.identity).encode())
        self.addCleanup(self.stop_workers)
        self.native_uci_fixture()

    def put(self, name, data, mode=0o600):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def run_engine(self, command, *args, data=None, ok=True):
        completed = subprocess.run(["sh", str(ENGINE), "--test-root", str(self.root), command, *args],
                                   input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=20, env=self.engine_env)
        if ok and completed.returncode:
            self.fail(f"{command}: {completed.stderr.decode()} (stdout {completed.stdout!r})")
        if not ok:
            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            return completed
        return json.loads(completed.stdout) if completed.stdout.strip() else None

    def fault_tool(self, name, needle="", output=b""):
        original = shutil.which(name, path=self.engine_env["PATH"])
        self.assertTrue(original)
        tools = self.root / "fault-tools"
        tools.mkdir(exist_ok=True)
        # Preserve the selected provider, including an emulated target binary,
        # before replacing its path. Unique copies also make stacked faults safe.
        fd, fallback = tempfile.mkstemp(prefix=name + "-under-test-", dir=tools)
        os.close(fd)
        shutil.copy2(original, fallback)
        needles = (needle,) if isinstance(needle, str) else tuple(needle)
        script = "#!" + shutil.which("python3") + "\nimport os, sys\n"
        script += "if any(needle in arg for needle in " + repr(needles) + " for arg in sys.argv[1:]):\n"
        script += "    os.write(1, " + repr(output) + ")\n    sys.exit(71)\n"
        script += "os.execv(" + repr(fallback) + ", [" + repr(original) + ", *sys.argv[1:]])\n"
        self.put("fault-tools/" + name, script.encode(), 0o755)
        self.engine_env["PATH"] = str(tools) + os.pathsep + self.engine_env["PATH"]

    def service(self, name="fixture", enabled=False, running=False, link_name=None):
        self.put(f"run/openwrt-test-services/{name}.running", str(running).lower().encode())
        path = self.put(f"etc/init.d/{name}", (f'''#!/bin/sh
base='{self.root}'
name='{name}'
action=$1
printf '%s %s\\n' "$name" "$action" >> "$base/run/service-actions"
if test -f "$base/run/$name.fail-$action"; then exit 1; fi
case "$action" in
 enabled) ls "$base/etc/rc.d"/S[0-9][0-9]"$name" >/dev/null 2>&1;;
 enable) ln -sf "../init.d/$name" "$base/etc/rc.d/S50$name";;
 disable) rm -f "$base/etc/rc.d"/S[0-9][0-9]"$name" "$base/etc/rc.d"/K[0-9][0-9]"$name";;
 reload|restart|start)
  if test "$action" = reload && test -f "$base/run/$name.reload-only"; then
   test "$(cat "$base/run/openwrt-test-services/$name.running")" = true || exit 1
  fi
  test ! -f "$base/run/$name.slow" || sleep 2
  if test -f "$base/run/$name.oneshot"; then
   echo false > "$base/run/openwrt-test-services/$name.running"
   echo true > "$base/run/$name.kernel-active"
  else echo true > "$base/run/openwrt-test-services/$name.running"; fi;;
 stop)
  echo false > "$base/run/openwrt-test-services/$name.running"
  test ! -f "$base/run/$name.oneshot" || echo false > "$base/run/$name.kernel-active";;
 *) exit 1;;
esac
''').encode(), 0o755)
        if enabled:
            (self.root / "etc/rc.d" / (link_name or f"S50{name}")).symlink_to(f"../init.d/{name}")
        return path

    def owned_service_script(self, enabled, running):
        name = "wrtix-managed-fixture"
        script = self.service(name, enabled=enabled, running=running)
        self.put("etc/wrtix/managed.json", json.dumps({"schema_version": 1,
                 "kind": "wrtix-owned-resources", "device_id": "fixture",
                 "files": ["etc/init.d/" + name], "services": [name]}).encode())
        return name, script

    def check(self, paths, services=()):
        return self.run_engine("check", data=json.dumps({"paths": list(paths), "services": list(services)}).encode())

    def test_package_preparation_installs_missing_only_and_retries_after_failure(self):
        tools = self.root / "package-tools"
        tools.mkdir()
        installed = self.put("run/installed-packages", b"existing\n")
        failure = self.root / "run/package-failure"
        script = f'''#!/bin/sh
case "$1" in
 update) exit 0;;
 info) grep -Fxq "$3" {shlex.quote(str(installed))};;
 add)
  shift
  [ "${1:-}" != --simulate ] || exit 0
  printf '%s\\n' "$@" >> {shlex.quote(str(self.root / 'run/package-actions'))}
  [ ! -f {shlex.quote(str(failure))} ] || exit 1
  printf '%s\\n' "$@" >> {shlex.quote(str(installed))};;
 *) exit 1;;
esac
'''
        self.put("package-tools/apk", script.encode(), 0o755)
        self.engine_env["PATH"] = str(tools) + os.pathsep + self.engine_env["PATH"]
        failure.touch()
        request = json.dumps(["existing", "new-package"]).encode()
        self.assertIn(b"package_install_failed", self.run_engine("package-prepare", data=request, ok=False).stderr)
        marker = self.root / "etc/wrtix/package-operation"
        self.assertEqual(marker.read_text().strip(), "prepare-packages")
        self.assertIn(b"package_operation_active", self.run_engine("identity", ok=False).stderr)
        self.run_engine("package-identity")
        failure.unlink()
        result = self.run_engine("package-prepare", data=request)
        self.assertEqual(result["state"], "packages-ready")
        self.assertFalse(marker.exists())
        self.assertEqual((self.root / "run/package-actions").read_text(), "new-package\nnew-package\n")
        self.run_engine("package-prepare", data=request)
        self.assertEqual((self.root / "run/package-actions").read_text(), "new-package\nnew-package\n")

    def test_package_preparation_rejects_pending_transaction_and_invalid_names(self):
        for package in ("-bad", "bad;command", "bad/name", ""):
            self.run_engine("package-prepare", data=json.dumps([package]).encode(), ok=False)
        token, archive, _ = self.bundle({"etc/example": (b"candidate", "600")})
        self.run_engine("stage", data=archive)
        result = self.run_engine("package-prepare", data=b'[]', ok=False)
        self.assertIn(b"transaction_active", result.stderr)
        self.assertFalse((self.root / "etc/wrtix/package-operation").exists())

    def test_opkg_preparation_skips_existing_packages(self):
        if shutil.which("apk", path=self.engine_env["PATH"]):
            self.skipTest("opkg fixture requires a host without APK")
        tools = self.root / "opkg-tools"
        tools.mkdir()
        installed = self.put("run/opkg-installed", b"existing\n")
        script = f'''#!/bin/sh
case "$1" in
 status)
  grep -Fxq "$2" {shlex.quote(str(installed))} || exit 1
  printf 'Status: install ok installed\\n';;
 update) exit 0;;
 install)
  shift
  printf '%s\\n' "$@" >> {shlex.quote(str(installed))};;
 *) exit 1;;
esac
'''
        self.put("opkg-tools/opkg", script.encode(), 0o755)
        self.engine_env["PATH"] = str(tools) + os.pathsep + self.engine_env["PATH"]
        request = b'["existing","new-package"]'
        result = self.run_engine("package-prepare", data=request)
        self.assertEqual(result["manager"], "opkg")
        self.run_engine("package-prepare", data=request)
        self.assertEqual(installed.read_text(), "existing\nnew-package\n")

    def bundle(self, files, services=None, baseline=None, extra=None, symlink=False, managed_services=None, initialize=False, resolver_target=None):
        files = {"etc/wrtix/identity.json": (json.dumps(self.identity).encode(), "600"), **files}
        services = services or {}
        baseline = baseline or self.check(files, services)
        token = uuid.uuid4().hex
        entries, payloads = [], {}
        for index, (path, value) in enumerate(files.items()):
            if value is None:
                entries.append({"path": path, "delete": True, "payload": None, "sha256": None,
                                "size": 0, "mode": None})
            else:
                data, mode = value
                member = f"payload/{index}"
                entries.append({"path": path, "delete": False, "payload": member,
                                "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mode": mode})
                payloads[member] = data
        manifest = {"schema_version": 1, "kind": "wrtix-transaction", "token": token,
                    "boot_id": baseline["boot_id"], "engine_sha256": hashlib.sha256(ENGINE.read_bytes()).hexdigest(),
                    "guard_sha256": hashlib.sha256(GUARD_SOURCE.read_bytes()).hexdigest(),
                    "device_id": "fixture",

                    "board_names": [], "initialize": initialize,
                    "contract_sha256": baseline["contract_sha256"], "ttl_seconds": 120,
                    "resolver_target": resolver_target,
                    "baseline": baseline, "files": entries, "services": services,
                    "managed_services": sorted(services) if managed_services is None else managed_services}
        payloads = {"manifest.json": json.dumps(manifest).encode(), **payloads}
        if extra:
            payloads.update(extra)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, data in payloads.items():
                info = tarfile.TarInfo(name)
                info.mode = 0o600
                info.size = len(data)
                if symlink and name.startswith("payload/"):
                    info.type = tarfile.SYMTYPE
                    info.linkname = "/etc/shadow"
                    info.size = 0
                    archive.addfile(info)
                else:
                    archive.addfile(info, io.BytesIO(data))
        return token, buffer.getvalue(), manifest

    def apply(self, files, services=None):
        token, data, manifest = self.bundle(files, services)
        self.run_engine("stage", data=data)
        self.run_engine("apply", token)
        return token, manifest

    def wait(self, predicate, seconds=10):
        deadline = time.monotonic() + seconds
        last = None
        while time.monotonic() < deadline:
            last = self.run_engine("status")
            if predicate(last):
                return last
            time.sleep(0.1)
        self.fail(f"target did not reach expected state: {last}")

    def ready(self):
        return self.wait(lambda status: status["state"] == "applied" and status["reload_complete"])

    def new_boot(self, *, watch=True):
        self.stop_workers()
        current = uuid.UUID((self.root / "proc/sys/kernel/random/boot_id").read_text().strip())
        value = str(uuid.UUID(int=current.int + 1))
        self.put("proc/sys/kernel/random/boot_id", (value + "\n").encode())
        self.put("proc/uptime", b"5.00 0.00\n")
        self.run_engine("early")
        if watch:
            self.start_watch()
        return value

    def start_watch(self):
        worker = subprocess.Popen(["sh", str(ENGINE), "--test-root", str(self.root), "watch"],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, env=self.engine_env)
        def reap():
            if worker.poll() is None:
                worker.terminate()
            try:
                worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)
        self.addCleanup(reap)

    def planned_reboot(self, token, remaining=90):
        armed = self.run_engine("reboot", token)
        self.assertEqual(armed["reboot_state"], "armed")
        current = self.new_boot()
        result = self.run_engine("resume", token, data=json.dumps(
            {"boot_id": current, "remaining_seconds": min(remaining, armed["reboot_remaining_seconds"])}).encode())
        self.assertEqual(result["reboot_state"], "resumed")
        return self.ready()

    def restored(self):
        """Fulfil an explicit rollback boot request before expecting completion."""
        request = self.root / "etc/wrtix/transactions/state/reboot-request"
        def finished(status):
            if request.exists():
                reason, requested_boot = request.read_text().split()
                current = (self.root / "proc/sys/kernel/random/boot_id").read_text().strip()
                if reason == "rollback" and requested_boot == current:
                    self.new_boot()
                    return False
            return status.get("state") == "rolled-back" and status.get("reload_complete")
        return self.wait(finished)

    def stop_workers(self):
        state = self.root / "etc/wrtix/transactions/state"
        for name in ("reload.child", "reload.pid", "watch.pid"):
            path = state / name
            if not path.exists():
                continue
            fields = path.read_text().split()
            if len(fields) != 4:
                continue
            pid = int(fields[0])
            proc = Path(f"/proc/{pid}/stat")
            if proc.exists():
                try:
                    actual = proc.read_text().split()
                    if actual[21] == fields[1] and pid != os.getpid():
                        os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        time.sleep(0.05)

    def native_uci_fixture(self):
        """Model native UCI show/get on private copies, without a host UCI dependency."""
        script = '''import pathlib,shlex,sys
base=pathlib.Path(BASE)
with (base/'run/uci-calls').open('a') as log: log.write(' '.join(sys.argv[1:])+'\\n')
args=sys.argv[1:];directory=None
while args and args[0].startswith('-'):
 option=args.pop(0)
 if option in ('-c','-C','-t'):
  value=args.pop(0)
  if option=='-c':directory=pathlib.Path(value)
 elif option not in ('-s','-q'):sys.exit(2)
try:
 package=args[1].split('.')[0]
 sections={};current=None;counts={}
 for line in (directory/package).read_text().splitlines():
  row=shlex.split(line,comments=True)
  if not row:continue
  if row[0]=='config' and len(row) in (2,3):
   kind=row[1];index=counts.get(kind,0);counts[kind]=index+1
   name=row[2] if len(row)==3 else '@'+kind+'['+str(index)+']'
   current={'kind':kind,'options':{}};sections[name]=current
  elif row[0] in ('option','list') and len(row)==3 and current is not None:
   if row[0]=='list':current['options'].setdefault(row[1],[]).append(row[2])
   else:current['options'][row[1]]=row[2]
  else:sys.exit(3)
 if args==['show',package]:
  for name,section in sections.items():
   print(package+'.'+name+'='+section['kind'])
   for key,value in section['options'].items():
    print(package+'.'+name+'.'+key+'='+repr(value))
 elif args[0]=='get' and args[1].startswith(package+'.'):
  if (base/'run/uci-fail-get').exists():sys.exit(4)
  name=args[1][len(package)+1:];section,sep,option=name.partition('.')
  value=sections[section]['options'][option] if sep else sections[section]['kind']
  print(' '.join(value) if isinstance(value,list) else value)
 else:sys.exit(2)
except (ValueError,KeyError,OSError):sys.exit(3)
'''
        script = "#!" + shutil.which("python3") + "\n" + script.replace("BASE", repr(str(self.root)), 1)
        tools = self.root / "fault-tools"
        self.put("fault-tools/uci", script.encode(), 0o755)
        self.engine_env["PATH"] = str(tools) + os.pathsep + os.environ["PATH"]

    @staticmethod
    def mac_network(address=None, kind="device"):
        value = "config " + kind + " 'mac_cpu'\n"
        value += " option name 'cpu'\n" if kind == "device" else " option device 'br-lan.1'\n"
        if address is not None:
            value += " option macaddr '" + address + "'\n"
        return value.encode()

    @staticmethod
    def mac_wireless(address=None, option=b"macaddr", *, radio=False):
        device = b"config wifi-device 'radio0'\n option path 'platform/fixture-radio'\n option channel '1'\n"
        interface = (b"config wifi-iface 'default_radio0'\n option device 'radio0'\n option ifname 'wlan0'\n"
                     b" option mode 'ap'\n option ssid 'fixture'\n")
        mac = b"" if address is None else b" option " + option + b" '" + address.encode() + b"'\n"
        return device + (mac if radio else b"") + interface + (b"" if radio else mac)

    def test_check_is_readonly_and_reports_absence_mode_and_service(self):
        self.put("etc/config/example", b"before\n", 0o640)
        self.service(enabled=True, running=True, link_name="S37fixture")
        result = self.check(["etc/config/example", "etc/example-new"], ["fixture"])
        self.assertEqual(result["modes"]["etc/config/example"], "640")
        self.assertIsNone(result["files"]["etc/example-new"])
        self.assertTrue(result["services"]["fixture"]["enabled"])
        self.assertTrue(result["services"]["fixture"]["running"])
        self.assertEqual(result["services"]["fixture"]["links"][0]["name"], "S37fixture")
        self.assertFalse((self.root / "etc/wrtix/transactions").exists())

    def test_invalid_service_link_cannot_be_hidden_by_json_aggregation(self):
        self.service()
        link = self.root / "etc/rc.d/S50fixture"
        link.symlink_to("../init.d/something-else")
        failed = self.run_engine("check", data=json.dumps({"paths": [], "services": ["fixture"]}).encode(), ok=False)
        self.assertIn(b"unsafe_service_link", failed.stderr)
        self.assertEqual(os.readlink(link), "../init.d/something-else")

    def test_service_snapshot_json_failure_cannot_return_partial_success(self):
        self.service()
        self.fault_tool("jq", "from_entries", b"{}\n")
        self.run_engine("check", data=json.dumps({"paths": [], "services": ["fixture"]}).encode(), ok=False)

    def test_final_snapshot_failure_cannot_be_overwritten_by_cleanup(self):
        self.fault_tool("jq", "(from_entries) as $f", b"{}\n")
        self.run_engine("check", data=json.dumps({"paths": [], "services": []}).encode(), ok=False)

    def test_file_hash_producer_failure_cannot_create_valid_looking_baseline(self):
        self.put("etc/config/example", b"before")
        self.fault_tool("sha256sum", "etc/config/example", ("a" * 64 + "  file\n").encode())
        failed = self.run_engine("check", data=json.dumps({"paths": ["etc/config/example"], "services": []}).encode(), ok=False)
        self.assertIn(b"file_snapshot_failed", failed.stderr)

    def test_file_mode_producer_failure_cannot_create_partial_baseline(self):
        self.put("etc/config/example", b"before")
        self.fault_tool("ls", "etc/config/example", b"-rw------- 1 0 0 6 Oct 28 00:00 file\n")
        failed = self.run_engine("check", data=json.dumps({"paths": ["etc/config/example"], "services": []}).encode(), ok=False)
        self.assertIn(b"file_snapshot_failed", failed.stderr)

    def test_empty_and_multiple_json_documents_are_rejected(self):
        valid = json.dumps({"paths": [], "services": []}).encode()
        for data in (b"", b"{}\n" + valid):
            with self.subTest(data=data):
                self.run_engine("check", data=data, ok=False)
        _, data, _ = self.bundle({}, extra={"manifest.json": b""})
        self.run_engine("stage", data=data, ok=False)

    def test_readback_json_failure_cannot_confirm_by_skipping_services(self):
        self.service()
        token, _ = self.apply({"etc/config/example": (b"after", "600")},
                              {"fixture": {"enable": True, "running": True, "action": "reload"}})
        self.ready()
        self.fault_tool("jq", ".value.running|tostring)]|@tsv")
        failed = self.run_engine("confirm", token, ok=False)
        self.assertIn(b"confirmation_readback_mismatch", failed.stderr)

    def test_service_only_deploy_and_removed_service_ownership(self):
        self.service(enabled=True, running=True)
        self.put("etc/wrtix/managed.json", json.dumps({"schema_version": 1,
                 "kind": "wrtix-owned-resources", "device_id": "fixture",
                 "files": [], "services": ["fixture"]}).encode())
        token, data, _ = self.bundle({}, {"fixture": {"enable": False, "running": False, "action": "restart"}},
                                     managed_services=[])
        self.run_engine("stage", data=data)
        self.run_engine("apply", token)
        self.ready()
        self.run_engine("confirm", token)
        ledger = json.loads((self.root / "etc/wrtix/managed.json").read_text())
        self.assertEqual(ledger["files"], ["etc/wrtix/identity.json"])
        self.assertEqual(ledger["services"], [])
        self.assertEqual((self.root / "run/openwrt-test-services/fixture.running").read_text().strip(), "false")

    def test_new_managed_service_can_be_staged_but_missing_service_cannot(self):
        path = "etc/init.d/wrtix-managed-new"
        services = {"wrtix-managed-new": {"enable": True, "running": None, "action": "restart"}}
        _, missing, _ = self.bundle({}, services)
        self.assertIn(b"missing_required_service", self.run_engine("stage", data=missing, ok=False).stderr)
        token, data, _ = self.bundle({path: (b"#!/bin/sh\nexit 0\n", "755")}, services)
        self.run_engine("stage", data=data)
        self.assertFalse((self.root / path).exists())
        self.run_engine("rollback", token)

    def test_init_deletion_rejects_enabled_or_running_service_before_mutation(self):
        for enabled, running, reason in ((True, True, b"init_service_must_be_disabled"),
                                         (False, True, b"init_service_must_be_stopped"),
                                         (False, False, b"init_service_links_present")):
            with self.subTest(enabled=enabled, running=running):
                name, script = self.owned_service_script(enabled, running)
                if not enabled and not running:
                    (self.root / ("etc/rc.d/K99" + name)).symlink_to("../init.d/" + name)
                token, data, _ = self.bundle({"etc/init.d/" + name: None},
                                            {name: {"enable": False, "running": False, "action": "restart"}},
                                            managed_services=[])
                failed = self.run_engine("stage", data=data, ok=False)
                self.assertIn(reason, failed.stderr)
                self.assertTrue(script.exists())
                self.assertEqual((self.root / ("run/openwrt-test-services/" + name + ".running")).read_text().strip(), str(running).lower())
                for link in (self.root / "etc/rc.d").iterdir():
                    link.unlink()

    def test_init_deletion_rechecks_runtime_before_first_apply_write(self):
        name, script = self.owned_service_script(False, False)
        token, data, _ = self.bundle({"etc/init.d/" + name: None})
        self.run_engine("stage", data=data)
        self.put("run/openwrt-test-services/" + name + ".running", b"true")
        failed = self.run_engine("apply", token, ok=False)
        self.assertIn(b"init_service_must_be_stopped", failed.stderr)
        self.assertTrue(script.exists())
        self.assertFalse((self.root / "etc/wrtix/transactions/state/applying").exists())
        self.run_engine("rollback", token)

    def test_confirmed_stop_then_init_deletion_leaves_no_daemon_or_links(self):
        name, script = self.owned_service_script(True, True)
        path = "etc/init.d/" + name
        token, _ = self.apply({path: (script.read_bytes(), "755")},
                              {name: {"enable": False, "running": False, "action": "restart"}})
        self.ready()
        self.run_engine("confirm", token)
        self.assertTrue(script.exists())
        self.assertEqual(list((self.root / "etc/rc.d").iterdir()), [])
        token, _ = self.apply({path: None})
        self.ready()
        self.run_engine("confirm", token)
        self.assertFalse(script.exists())
        self.assertEqual(list((self.root / "etc/rc.d").iterdir()), [])
        self.assertEqual((self.root / ("run/openwrt-test-services/" + name + ".running")).read_text().strip(), "false")
        ledger = json.loads((self.root / "etc/wrtix/managed.json").read_text())
        self.assertEqual(ledger["files"], ["etc/wrtix/identity.json"])
        self.assertEqual(ledger["services"], [])

    def test_missing_disabled_init_does_not_hide_running_daemon_at_confirmation(self):
        self.service(enabled=False, running=False)
        token, _ = self.apply({"etc/config/example": (b"after", "600")},
                              {"fixture": {"enable": False, "running": False, "action": "restart"}})
        self.ready()
        (self.root / "etc/init.d/fixture").unlink()
        self.put("run/openwrt-test-services/fixture.running", b"true")
        failed = self.run_engine("confirm", token, ok=False)
        self.assertIn(b"confirmation_readback_mismatch", failed.stderr)

    def test_managed_service_ownership_must_be_unique_declared_subset(self):
        for names in (["unknown"], ["fixture", "fixture"]):
            with self.subTest(names=names):
                _, data, _ = self.bundle({}, {"fixture": {"enable": False, "running": False, "action": "reload"}}, managed_services=names)
                failed = self.run_engine("stage", data=data, ok=False)
                self.assertIn(b"invalid_manifest", failed.stderr)

    def test_apply_confirm_files_modes_and_real_service_readback(self):
        self.put("etc/config/example", b"before\n", 0o640)
        self.service()
        token, _ = self.apply({"etc/config/example": (b"after\n", "644"),
                               "etc/fixture-secret": (b"synthetic-secret\n", "600")},
                              {"fixture": {"enable": True, "running": True, "action": "reload"}})
        self.ready()
        result = self.run_engine("confirm", token)
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual((self.root / "etc/config/example").read_bytes(), b"after\n")
        self.assertEqual(stat.S_IMODE((self.root / "etc/config/example").stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE((self.root / "etc/fixture-secret").stat().st_mode), 0o600)
        ledger = json.loads((self.root / "etc/wrtix/managed.json").read_text())
        self.assertEqual(set(ledger["files"]), {"etc/config/example", "etc/fixture-secret", "etc/wrtix/identity.json"})

    def test_reload_only_daemon_is_started_when_required_running(self):
        self.service(enabled=False, running=False)
        self.put("run/fixture.reload-only", b"reload needs an existing daemon")
        token, _ = self.apply({"etc/config/example": (b"after", "600")},
                              {"fixture": {"enable": True, "running": True, "action": "reload"}})
        self.ready()
        self.assertEqual(self.run_engine("confirm", token)["state"], "confirmed")
        actions = (self.root / "run/service-actions").read_text()
        self.assertIn("fixture start\n", actions)
        self.assertNotIn("fixture reload\n", actions)

    def test_failed_start_cannot_confirm_and_rollback_restores_stopped_baseline(self):
        self.put("etc/config/example", b"original")
        self.service(enabled=False, running=False)
        self.put("run/fixture.reload-only", b"reload needs an existing daemon")
        self.put("run/fixture.fail-start", b"synthetic startup failure")
        token, _ = self.apply({"etc/config/example": (b"after", "600")},
                              {"fixture": {"enable": True, "running": True, "action": "reload"}})
        status = self.wait(lambda result: result["state"] == "rolled-back" and result["reload_complete"])
        self.run_engine("confirm", token, ok=False)
        self.assertLess(status["now"], status["deadline"])
        self.assertEqual((self.root / "etc/wrtix/transactions/state/rolled-back").read_text().strip(), "activation_failed")
        self.assertEqual((self.root / "etc/config/example").read_bytes(), b"original")
        self.assertEqual((self.root / "run/openwrt-test-services/fixture.running").read_text().strip(), "false")
        self.assertEqual(list((self.root / "etc/rc.d").iterdir()), [])

    def test_activation_and_restore_keep_first_safe_child_failures(self):
        self.fault_tool("jq", needle=("test(", "match(", "sub("))
        self.put("etc/config/example", b"original")
        service = self.service(enabled=True, running=True)
        source = service.read_text()
        source = source.replace('case "$action" in', """if test "$action" = restart; then
 if test "$(cat "$base/etc/config/example")" = candidate; then
  printf 'private-child-stderr-sentinel\\n' >&2; exit 42
 elif test -f "$base/run/fail-restore"; then
  printf 'private-child-stderr-sentinel\\n' >&2
  restore_code=$(cat "$base/run/fail-restore")
  printf '%s\\n' "$restore_code" >> "$base/run/restore-failures"
  exit "$restore_code"
 fi
fi
case "$action" in""")
        service.write_text(source)
        self.put("run/fail-restore", b"77")
        token, _ = self.apply({"etc/config/example": (b"candidate", "600")},
                              {"fixture": {"enable": True, "running": True, "action": "restart"}})
        result = self.wait(lambda value: value["state"] == "rollback_incomplete")
        expected = {"phase": "activate", "code": "service_reload_failed", "kind": "service",
                    "service": "fixture", "action": "restart", "exit_code": 42, "exit_kind": "exit"}
        self.assertEqual(result["activation_error"], expected)
        expected_restore = {**expected, "phase": "restore", "code": "service_restore_failed", "exit_code": 77}
        self.assertEqual(result["restore_error"], expected_restore)
        # A later retry may fail differently; it must not erase either first cause.
        self.put("run/fail-restore", b"78")
        self.wait(lambda _: "78" in (self.root / "run/restore-failures").read_text().splitlines())
        self.assertEqual(self.run_engine("status")["restore_error"], expected_restore)
        (self.root / "run/fail-restore").unlink()
        result = self.wait(lambda value: value["state"] == "rolled-back" and value["reload_complete"])
        self.assertEqual(result["activation_error"], expected)
        self.assertEqual(result["restore_error"], expected_restore)
        self.assertNotIn("private-child-stderr-sentinel", json.dumps(result))
        state = self.root / "etc/wrtix/transactions/state"
        self.assertNotIn(b"private-child-stderr-sentinel", b"".join(file.read_bytes() for file in state.glob("*error*")))
        # Corrupt/unrecognized metadata is never presented as a trusted diagnostic.
        self.put("etc/wrtix/transactions/state/activate-error.json", json.dumps(
            {**expected, "service": "private value with spaces"}).encode())
        self.assertIsNone(self.run_engine("status")["activation_error"])
        self.assertEqual((self.root / "etc/config/example").read_bytes(), b"original")
        self.run_engine("confirm", token, ok=False)

    def test_reload_only_daemon_is_started_when_rollback_restores_running(self):
        self.service(enabled=True, running=True, link_name="S37fixture")
        self.put("run/fixture.reload-only", b"reload needs an existing daemon")
        self.put("etc/config/example", b"original", 0o640)
        token, _ = self.apply({"etc/config/example": (b"after", "600")},
                              {"fixture": {"enable": False, "running": False, "action": "reload", "reload": False}})
        self.ready()
        before = (self.root / "run/service-actions").read_text()
        self.run_engine("rollback", token)
        self.wait(lambda x: x["state"] == "rolled-back" and x["reload_complete"])
        actions = (self.root / "run/service-actions").read_text()[len(before):]
        self.assertIn("fixture start\n", actions)
        self.assertNotIn("fixture reload\n", actions)
        self.assertEqual((self.root / "etc/config/example").read_bytes(), b"original")
        self.assertEqual(stat.S_IMODE((self.root / "etc/config/example").stat().st_mode), 0o640)
        self.assertEqual(sorted(p.name for p in (self.root / "etc/rc.d").iterdir()), ["S37fixture"])

    def test_confirmation_rechecks_file_modes_and_service_runtime(self):
        self.service()
        token, _ = self.apply({"etc/config/example": (b"after", "600")},
                              {"fixture": {"enable": True, "running": True, "action": "reload"}})
        self.ready()
        (self.root / "etc/config/example").chmod(0o644)
        failed = self.run_engine("confirm", token, ok=False)
        self.assertIn(b"confirmation_readback_mismatch", failed.stderr)
        (self.root / "etc/config/example").chmod(0o600)
        self.put("run/openwrt-test-services/fixture.running", b"false")
        failed = self.run_engine("confirm", token, ok=False)
        self.assertIn(b"confirmation_readback_mismatch", failed.stderr)
        self.put("run/openwrt-test-services/fixture.running", b"true")
        self.assertEqual(self.run_engine("confirm", token)["state"], "confirmed")

    def test_manual_rollback_restores_absence_modes_links_and_running(self):
        self.put("etc/config/example", b"before\n", 0o640)
        self.service(enabled=True, running=True, link_name="S37fixture")
        token, _ = self.apply({"etc/config/example": (b"after\n", "644"), "etc/fixture-new": (b"new", "600")},
                              {"fixture": {"enable": False, "running": False, "action": "reload"}})
        self.ready()
        self.run_engine("rollback", token)
        self.wait(lambda x: x["state"] == "rolled-back" and x["reload_complete"])
        self.assertEqual((self.root / "etc/config/example").read_bytes(), b"before\n")
        self.assertEqual(stat.S_IMODE((self.root / "etc/config/example").stat().st_mode), 0o640)
        self.assertFalse((self.root / "etc/fixture-new").exists())
        self.assertFalse((self.root / "etc/wrtix/managed.json").exists())
        links = sorted(p.name for p in (self.root / "etc/rc.d").iterdir())
        self.assertEqual(links, ["S37fixture"])
        self.assertEqual((self.root / "run/openwrt-test-services/fixture.running").read_text().strip(), "true")

    def test_owned_deletion_is_reversed_by_rollback(self):
        self.put("etc/old-plugin", b"old contents", 0o640)
        self.put("etc/wrtix/managed.json", json.dumps({"schema_version": 1,
                 "kind": "wrtix-owned-resources", "device_id": "fixture",
                 "files": ["etc/old-plugin"], "services": []}).encode())
        token, _ = self.apply({"etc/old-plugin": None})
        self.ready()
        self.assertFalse((self.root / "etc/old-plugin").exists())
        self.run_engine("rollback", token)
        self.wait(lambda x: x["state"] == "rolled-back" and x["reload_complete"])
        self.assertEqual((self.root / "etc/old-plugin").read_bytes(), b"old contents")
        self.assertEqual(stat.S_IMODE((self.root / "etc/old-plugin").stat().st_mode), 0o640)

    def test_unowned_deletion_rejected_before_mutation(self):
        self.put("etc/unowned", b"keep")
        _, data, _ = self.bundle({"etc/unowned": None})
        failed = self.run_engine("stage", data=data, ok=False)
        self.assertIn(b"deletion_not_owned", failed.stderr)
        self.assertEqual((self.root / "etc/unowned").read_bytes(), b"keep")

    def test_removed_runtime_mode_cannot_skip_agent_checks(self):
        for flag in (True, False):
            with self.subTest(runtime_mode=flag):
                request={"paths":[],"services":[],"native_runtime":flag}
                failed=self.run_engine("check",data=json.dumps(request).encode(),ok=False)
                self.assertIn(b"invalid_check_request",failed.stderr)
                _,_,manifest=self.bundle({"etc/example":(b"candidate","600")})
                manifest["native_runtime"]=flag
                _,data,_=self.bundle({"etc/example":(b"candidate","600")},
                                     extra={"manifest.json":json.dumps(manifest).encode()})
                failed=self.run_engine("stage",data=data,ok=False)
                self.assertIn(b"invalid_manifest",failed.stderr)
                self.assertFalse((self.root/"etc/example").exists())

    def test_engine_digest_rejection_preserves_config_and_terminal_journal(self):
        token, _ = self.apply({"etc/config/example": (b"accepted", "600")})
        self.ready()
        self.run_engine("confirm", token)
        journal = self.root / "etc/wrtix/transactions/state/manifest.json"
        original = journal.read_bytes()
        for value, reason in ((None, b"invalid_manifest"), ("malformed", b"invalid_manifest"),
                              ("0" * 64, b"engine_version_changed")):
            with self.subTest(engine_digest=value):
                _, _, manifest = self.bundle({"etc/config/example": (b"candidate", "600")})
                if value is None:
                    del manifest["engine_sha256"]
                else:
                    manifest["engine_sha256"] = value
                _, data, _ = self.bundle({"etc/config/example": (b"candidate", "600")},
                                         extra={"manifest.json": json.dumps(manifest).encode()})
                failed = self.run_engine("stage", data=data, ok=False)
                self.assertIn(reason, failed.stderr)
                self.assertEqual(journal.read_bytes(), original)
                self.assertEqual((self.root / "etc/config/example").read_bytes(), b"accepted")
                self.assertEqual(self.run_engine("status")["token"], token)

    def test_guard_digest_rejection_preserves_config_and_terminal_journal(self):
        token, _ = self.apply({"etc/config/example": (b"accepted", "600")})
        self.ready()
        self.run_engine("confirm", token)
        journal = self.root / "etc/wrtix/transactions/state/manifest.json"
        original = journal.read_bytes()
        for value, reason in ((None, b"invalid_manifest"), ("malformed", b"invalid_manifest"),
                              ("0" * 64, b"guard_version_changed"), ("installed_drift", b"guard_version_changed")):
            with self.subTest(guard_digest=value):
                _, _, manifest = self.bundle({"etc/config/example": (b"candidate", "600")})
                if value is None:
                    del manifest["guard_sha256"]
                elif value == "installed_drift":
                    self.put("etc/init.d/wrtix-agent", b"#!/bin/sh\nexit 0\n", 0o755)
                else:
                    manifest["guard_sha256"] = value
                _, data, _ = self.bundle({"etc/config/example": (b"candidate", "600")},
                                         extra={"manifest.json": json.dumps(manifest).encode()})
                failed = self.run_engine("stage", data=data, ok=False)
                self.assertIn(reason, failed.stderr)
                self.assertEqual(journal.read_bytes(), original)
                self.assertEqual((self.root / "etc/config/example").read_bytes(), b"accepted")
                self.assertEqual(self.run_engine("status")["token"], token)

    def test_reserved_guard_service_cannot_be_managed_or_stop_rollback(self):
        token, _ = self.apply({"etc/config/example": (b"accepted", "600")})
        self.ready()
        self.run_engine("confirm", token)
        journal = self.root / "etc/wrtix/transactions/state/manifest.json"
        original = journal.read_bytes()
        _, _, manifest = self.bundle({"etc/config/example": (b"candidate", "600")})
        manifest["services"] = {"wrtix-agent": {"enable": False, "running": False, "action": "reload"}}
        manifest["managed_services"] = ["wrtix-agent"]
        _, data, _ = self.bundle({"etc/config/example": (b"candidate", "600")},
                                 extra={"manifest.json": json.dumps(manifest).encode()})
        failed = self.run_engine("stage", data=data, ok=False)
        self.assertIn(b"invalid_manifest", failed.stderr)
        self.assertEqual(journal.read_bytes(), original)
        self.assertEqual((self.root / "etc/config/example").read_bytes(), b"accepted")
        self.assertEqual((self.root / "etc/init.d/wrtix-agent").read_bytes(), GUARD_SOURCE.read_bytes())

    def test_stage_cas_rejects_file_drift(self):
        self.put("etc/config/example", b"before")
        _, data, _ = self.bundle({"etc/config/example": (b"after", "600")})
        self.put("etc/config/example", b"human change")
        failed = self.run_engine("stage", data=data, ok=False)
        self.assertIn(b"baseline_changed", failed.stderr)
        self.assertEqual((self.root / "etc/config/example").read_bytes(), b"human change")

    def test_apply_cas_rejects_poststage_drift(self):
        self.put("etc/config/example", b"before")
        token, data, _ = self.bundle({"etc/config/example": (b"after", "600")})
        self.run_engine("stage", data=data)
        self.put("etc/config/example", b"human change")
        failed = self.run_engine("apply", token, ok=False)
        self.assertIn(b"baseline_changed", failed.stderr)
        self.run_engine("rollback", token)
        self.assertEqual((self.root / "etc/config/example").read_bytes(), b"human change")

    def test_paths_parent_symlinks_and_reserved_destinations_rejected(self):
        for path in ("etc/../shadow", "etc//shadow", "/etc/config/test", "etc/shadow",
                     "etc/ssh/ssh_host_ed25519_key", "etc/init.d/wrtix-agent"):
            with self.subTest(path=path):
                self.run_engine("check", data=json.dumps({"paths": [path], "services": []}).encode(), ok=False)
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "etc/link").symlink_to(outside, target_is_directory=True)
        self.run_engine("check", data=json.dumps({"paths": ["etc/link/file"], "services": []}).encode(), ok=False)

    def test_archive_extras_and_symlinks_rejected(self):
        _, data, _ = self.bundle({"etc/config/example": (b"after", "600")}, extra={"extra": b"bad"})
        self.run_engine("stage", data=data, ok=False)
        _, data, _ = self.bundle({"etc/config/example": (b"after", "600")}, symlink=True)
        failed = self.run_engine("stage", data=data, ok=False)
        self.assertIn(b"nonregular_archive_member", failed.stderr)
        self.assertFalse((self.root / "etc/config/example").exists())

    def test_confirmation_requires_apply_and_reload_completion(self):
        token, data, _ = self.bundle({"etc/config/example": (b"after", "600")})
        self.run_engine("stage", data=data)
        failed = self.run_engine("confirm", token, ok=False)
        self.assertIn(b"reload_not_complete", failed.stderr)
        self.run_engine("rollback", token)

    def test_candidate_corruption_is_not_installed(self):
        self.put("etc/config/example", b"before")
        token, data, _ = self.bundle({"etc/config/example": (b"after", "600")})
        self.run_engine("stage", data=data)
        self.put("etc/wrtix/transactions/state/candidate/0", b"corrupt")
        failed = self.run_engine("apply", token, ok=False)
        self.assertIn(b"payload_corrupt", failed.stderr)
        self.run_engine("rollback", token)
        self.wait(lambda x: x["state"] == "rolled-back" and x["reload_complete"])
        self.assertEqual((self.root / "etc/config/example").read_bytes(), b"before")

    def test_config_dependent_activation_failure_rolls_back_before_deadline(self):
        self.put("etc/config/example", b"accepted\n", 0o640)
        service = self.service(enabled=True, running=True, link_name="S37fixture")
        script = service.read_text().replace('action=$1\n', 'action=$1\nif test "$action" = reload && test "$(cat "$base/etc/config/example")" = rejected; then\n echo false > "$base/run/openwrt-test-services/$name.running"\n exit 1\nfi\n')
        service.write_text(script)
        token, _ = self.apply({"etc/config/example": (b"rejected\n", "600")},
                              {"fixture": {"enable": True, "running": True, "action": "reload"}})
        status = self.wait(lambda result: result["state"] == "rolled-back" and result["reload_complete"])
        self.assertLess(status["now"], status["deadline"])
        self.assertEqual((self.root / "proc/uptime").read_bytes(), b"100.00 0.00\n")
        self.assertEqual((self.root / "etc/wrtix/transactions/state/rolled-back").read_text().strip(), "activation_failed")
        self.assertEqual((self.root / "etc/config/example").read_bytes(), b"accepted\n")
        self.assertEqual(stat.S_IMODE((self.root / "etc/config/example").stat().st_mode), 0o640)
        self.assertEqual((self.root / "run/openwrt-test-services/fixture.running").read_text().strip(), "true")
        self.assertEqual(sorted(p.name for p in (self.root / "etc/rc.d").iterdir()), ["S37fixture"])
        self.run_engine("confirm", token, ok=False)

    def test_service_failure_prevents_confirmation_and_allows_rollback(self):
        self.put("etc/config/example", b"before")
        self.service(enabled=True, running=True)
        self.put("run/fixture.fail-reload", b"fail")
        token, _ = self.apply({"etc/config/example": (b"after", "600")},
                              {"fixture": {"enable": True, "running": True, "action": "reload"}})
        self.wait(lambda result: result["state"] == "rollback_incomplete")
        self.run_engine("confirm", token, ok=False)
        self.assertEqual((self.root / "etc/config/example").read_bytes(), b"before")
        (self.root / "run/fixture.fail-reload").unlink()
        status = self.wait(lambda x: x["state"] == "rolled-back" and x["reload_complete"])
        self.assertLess(status["now"], status["deadline"])
        self.assertEqual((self.root / "etc/config/example").read_bytes(), b"before")

    def test_expiry_rolls_back_without_controller_connection(self):
        self.put("etc/config/example", b"before")
        self.apply({"etc/config/example": (b"after", "600")})
        self.ready()
        self.put("proc/uptime", b"500.00 0.00\n")
        self.wait(lambda x: x["state"] == "rolled-back" and x["reload_complete"])
        self.assertEqual((self.root / "etc/config/example").read_bytes(), b"before")

    def test_new_boot_id_causes_guard_rollback(self):
        self.put("etc/config/example", b"before")
        self.apply({"etc/config/example": (b"after", "600")})
        self.ready()
        self.put("proc/sys/kernel/random/boot_id", b"00000000-0000-4000-8000-000000000002\n")
        self.wait(lambda x: x["state"] == "rolled-back" and x["reload_complete"])
        self.assertEqual((self.root / "etc/config/example").read_bytes(), b"before")

    def test_failed_restore_blocks_new_stage_and_recovers_after_service_fix(self):
        self.service(enabled=True, running=True)
        token, data, _ = self.bundle({"etc/config/example": (b"after", "600")},
                                    {"fixture": {"enable": False, "running": False, "action": "restart"}})
        self.run_engine("stage", data=data)
        self.run_engine("apply", token)
        self.ready()
        self.put("run/fixture.fail-restart", b"fail")
        self.run_engine("rollback", token)
        self.wait(lambda x: x["state"] == "rollback_incomplete")
        failed = self.run_engine("stage", data=data, ok=False)
        self.assertIn(b"transaction_active", failed.stderr)
        (self.root / "run/fixture.fail-restart").unlink()
        self.wait(lambda x: x["state"] == "rolled-back" and x["reload_complete"])

    def test_rollback_honors_declared_service_restart_action(self):
        self.service(enabled=True, running=True)
        token, _ = self.apply({"etc/config/example": (b"after", "600")},
                              {"fixture": {"enable": False, "running": False, "action": "restart"}})
        self.ready()
        before = (self.root / "run/service-actions").read_text()
        self.run_engine("rollback", token)
        self.wait(lambda x: x["state"] == "rolled-back" and x["reload_complete"])
        actions = (self.root / "run/service-actions").read_text()[len(before):]
        self.assertIn("fixture restart", actions)
        self.assertNotIn("fixture reload", actions)

    def test_unrelated_plugin_change_does_not_reload_wifi_or_running_service(self):
        self.service(enabled=True, running=True)
        self.put("etc/shadow", ("root:" + HASH + ":1:0:99999:7:::\n").encode())
        self.put("etc/wrtix/root-password-hash", (HASH + "\n").encode())
        (self.root / "etc/resolv.conf").symlink_to("/tmp/resolv.conf.d/resolv.conf.auto")
        self.put("sbin/wifi", (f"#!/bin/sh\necho called >> '{self.root}/run/wifi-actions'\n").encode(), 0o755)
        shadow_inode = (self.root / "etc/shadow").stat().st_ino
        token, _ = self.apply({"etc/vendor-config.json": (b'{"enabled":true}\n', "600")},
                              {"fixture": {"enable": True, "running": True, "action": "reload", "reload": False}})
        self.ready()
        self.assertFalse((self.root / "run/wifi-actions").exists())
        self.assertNotIn("fixture reload", (self.root / "run/service-actions").read_text())
        self.assertEqual((self.root / "etc/shadow").stat().st_ino, shadow_inode)
        self.assertEqual(self.run_engine("confirm", token)["state"], "confirmed")

    def test_rollback_never_exposes_candidate_completion_as_restored(self):
        token, _ = self.apply({"etc/config/example": (b"candidate", "600")})
        self.ready()
        real_mv = shutil.which("mv", path=self.engine_env["PATH"])
        gate = self.root / "run/rollback-publication"
        release = self.root / "run/rollback-continue"
        wrapper = "#!" + shutil.which("python3") + "\n" + """import pathlib,subprocess,sys,time
result=subprocess.run([MV,*sys.argv[1:]])
if sys.argv[-1].endswith('/state/rolled-back'):
 pathlib.Path(GATE).touch()
 deadline=time.monotonic()+5
 while not pathlib.Path(RELEASE).exists() and time.monotonic()<deadline:time.sleep(.01)
sys.exit(result.returncode)
""".replace("MV", repr(real_mv)).replace("GATE", repr(str(gate))).replace("RELEASE", repr(str(release)))
        self.put("fault-tools/mv", wrapper.encode(), 0o755)
        worker = subprocess.Popen(["sh", str(ENGINE), "--test-root", str(self.root), "rollback", token],
                                  env=self.engine_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not gate.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(gate.exists())
            self.assertFalse(self.run_engine("status")["reload_complete"])
        finally:
            release.touch()
            stdout, stderr = worker.communicate(timeout=10)
        self.assertEqual(worker.returncode, 0, stderr)
        self.restored()

    def test_firmware_operation_blocks_stage(self):
        _, data, _ = self.bundle({"etc/config/example": (b"after", "600")})
        self.put("tmp/sysupgrade-trigger", b"active")
        failed = self.run_engine("stage", data=data, ok=False)
        self.assertIn(b"firmware_operation_active", failed.stderr)
        self.assertFalse((self.root / "etc/config/example").exists())

    def test_corrupt_backup_does_not_partially_restore_other_files(self):
        self.put("etc/config/example", b"before")
        token, _ = self.apply({"etc/config/example": (b"after", "600"), "etc/fixture-new": (b"new", "600")})
        self.ready()
        state = self.root / "etc/wrtix/transactions/state"
        rows = json.loads((state / "before.json").read_text())
        row = next(x for x in rows if x["path"] == "etc/config/example")
        (state / "backup" / str(row["index"])).write_bytes(b"corrupt")
        failed = self.run_engine("rollback", token, ok=False)
        self.assertIn(b"corrupt_backup", failed.stderr)
        self.assertEqual((self.root / "etc/config/example").read_bytes(), b"after")
        self.assertEqual((self.root / "etc/fixture-new").read_bytes(), b"new")

    def test_killed_reload_worker_is_recovered_and_orphan_stopped(self):
        self.service()
        self.put("run/fixture.slow", b"slow")
        token, _ = self.apply({"etc/config/example": (b"after", "600")},
                              {"fixture": {"enable": True, "running": True, "action": "reload"}})
        state = self.root / "etc/wrtix/transactions/state"
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if (state / "reload.child").exists() and (state / "reload.pid").exists():
                pid, ticks, _, _ = (state / "reload.pid").read_text().split()
                proc = Path(f"/proc/{pid}/stat")
                if proc.exists() and proc.read_text().split()[21] == ticks:
                    os.kill(int(pid), signal.SIGKILL)
                    break
            time.sleep(0.03)
        else:
            self.fail("reload worker not observed")
        (self.root / "run/fixture.slow").unlink()
        self.ready()
        self.assertTrue((state / "reload-recoveries").exists())
        self.assertEqual(self.run_engine("confirm", token)["state"], "confirmed")


    def test_plain_enrollment_preserves_existing_network_and_credentials(self):
        (self.root / "etc/wrtix/identity.json").unlink()
        self.put("etc/shadow", b"root:EXISTING:0:0:99999:7:::\n")
        self.put("etc/config/network", b"config interface 'uplink'\n option proto 'dhcp'\n")
        self.put("etc/dropbear/authorized_keys", b"existing-key\n")
        token, data, _ = self.bundle({"etc/config/example": (b"config example 'x'\n", "600")}, initialize=True)
        self.run_engine("stage", data=data)
        self.run_engine("apply", token)
        state = self.ready()
        self.assertFalse(state["reboot_required"])
        self.run_engine("confirm", token)
        self.assertEqual((self.root / "etc/shadow").read_bytes(), b"root:EXISTING:0:0:99999:7:::\n")
        self.assertEqual((self.root / "etc/dropbear/authorized_keys").read_bytes(), b"existing-key\n")
        self.assertIn(b"uplink", (self.root / "etc/config/network").read_bytes())

    def test_enrollment_expiry_restores_unregistered_identity(self):
        (self.root / "etc/wrtix/identity.json").unlink()
        token, data, _ = self.bundle({"etc/new-setting": (b"candidate", "600")}, initialize=True)
        self.run_engine("stage", data=data)
        self.run_engine("apply", token)
        self.ready()
        self.put("proc/uptime", b"500.00 0.00\n")
        self.restored()
        self.assertFalse((self.root / "etc/wrtix/identity.json").exists())
        self.assertFalse((self.root / "etc/new-setting").exists())
        self.assertEqual(self.run_engine("identity")["identity"]["device_id"], "")

    def test_unregistered_target_requires_explicit_initialization(self):
        (self.root / "etc/wrtix/identity.json").unlink()
        _, data, _ = self.bundle({})
        failed = self.run_engine("stage", data=data, ok=False)
        self.assertIn(b"baseline_changed", failed.stderr)
        self.assertFalse((self.root / "etc/wrtix/transactions/state").exists())

    def test_package_operation_blocks_stage(self):
        _, data, _ = self.bundle({})
        self.put("etc/wrtix/package-operation", b"install\n")
        result = self.run_engine("stage", data=data, ok=False)
        self.assertIn(b"package_operation_active", result.stderr)

    def test_optional_password_rolls_back_exact_shadow(self):
        old = b"root:EXISTING:0:0:99999:7:::\nservice:!:0:0:99999:7:::\n"
        self.put("etc/shadow", old)
        token, _ = self.apply({"etc/wrtix/root-password-hash": ((HASH + "\n").encode(), "600")})
        self.ready()
        self.assertIn(HASH.encode(), (self.root / "etc/shadow").read_bytes())
        self.run_engine("rollback", token)
        self.restored()
        self.assertEqual((self.root / "etc/shadow").read_bytes(), old)
        self.assertFalse((self.root / "etc/wrtix/root-password-hash").exists())

    def with_check(self, phase, script=b"#!/bin/sh\nexit 0\n", digest=None):
        files = {"usr/libexec/wrtix-managed/check-example": (script, "700")}
        token, _, manifest = self.bundle(files)
        manifest["checks"] = [{"name": "example", "phase": phase,
            "executable": "/usr/libexec/wrtix-managed/check-example",
            "sha256": digest or hashlib.sha256(script).hexdigest(), "timeoutSeconds": 2,
            "input": {"private": "do-not-log-this"}}]
        entries = {"manifest.json": json.dumps(manifest).encode(),
                   "payload/0": json.dumps(self.identity).encode(), "payload/1": script}
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for path, value in entries.items():
                info = tarfile.TarInfo(path); info.mode=0o600; info.size=len(value)
                archive.addfile(info, io.BytesIO(value))
        return token, stream.getvalue(), manifest

    def test_failed_preflight_has_no_managed_writes_or_secret_output(self):
        _, data, _ = self.with_check("preflight", b"#!/bin/sh\ncat\nexit 1\n")
        failed = self.run_engine("stage", data=data, ok=False)
        self.assertIn(b"preflight_check_failed", failed.stderr)
        self.assertNotIn(b"do-not-log-this", failed.stderr + failed.stdout)
        self.assertFalse((self.root / "usr/libexec/wrtix-managed/check-example").exists())

    def test_check_hash_cannot_be_bypassed(self):
        _, data, _ = self.with_check("preflight", digest="a"*64)
        self.run_engine("stage", data=data, ok=False)

    def test_failed_preconfirm_cannot_commit(self):
        token, data, _ = self.with_check("preconfirm", b"#!/bin/sh\nexit 1\n")
        self.run_engine("stage", data=data)
        self.run_engine("apply", token)
        self.ready()
        self.run_engine("confirm", token, ok=False)
        self.assertEqual(self.run_engine("status")["state"], "applied")
        self.run_engine("rollback", token)
        self.restored()

    def test_installed_verify_rechecks_hash_and_check_result(self):
        token, data, manifest = self.with_check("preconfirm")
        self.run_engine("stage", data=data)
        self.run_engine("apply", token)
        self.ready()
        self.run_engine("confirm", token)
        request = json.dumps({"checks": manifest["checks"]}).encode()
        self.assertTrue(self.run_engine("verify", data=request)["matches"])
        self.put("usr/libexec/wrtix-managed/check-example", b"#!/bin/sh\nexit 0\n# drift\n", 0o700)
        self.run_engine("verify", data=request, ok=False)

    def test_network_mac_change_requires_boot_and_keeps_deadline(self):
        self.service("network", enabled=True, running=True)
        self.put("etc/config/network", self.mac_network("02:00:00:00:00:01"))
        token, _ = self.apply({"etc/config/network": (self.mac_network("02:00:00:00:00:02"), "600")},
                              {"network": {"enable": True, "running": True, "action": "reload"}})
        status = self.ready()
        self.assertTrue(status["reboot_required"])
        self.run_engine("confirm", token, ok=False)
        result = self.planned_reboot(token)
        deadline = result["deadline"]
        retry = self.run_engine("resume", token, data=json.dumps({"boot_id": result["current_boot_id"], "remaining_seconds": 120}).encode())
        self.assertEqual(retry["deadline"], deadline)
        self.run_engine("confirm", token)

    def test_mac_rollback_restores_via_baseline_boot_without_extra_restart(self):
        self.service("network", enabled=True, running=True)
        before = self.mac_network("02:00:00:00:00:01")
        self.put("etc/config/network", before)
        token, _ = self.apply({"etc/config/network": (self.mac_network("02:00:00:00:00:02"), "600")},
                              {"network": {"enable": True, "running": True, "action": "reload"}})
        self.ready()
        self.planned_reboot(token)
        self.run_engine("rollback", token)
        self.restored()
        self.assertEqual((self.root / "etc/config/network").read_bytes(), before)
        actions = (self.root / "run/service-actions").read_text().splitlines()
        self.assertFalse(any(line in ("network restart", "network reload") for line in actions))

if __name__ == "__main__":
    unittest.main()
