"""Package prerequisites and the explicitly requested preparation phase."""
import shlex


def missing_packages(transport, packages):
    words = " ".join(shlex.quote(package) for package in sorted(set(packages)))
    if not words:
        return []
    script = """set -eu
for package in WORDS; do
 if command -v apk >/dev/null 2>&1; then
  apk info -e "$package" >/dev/null 2>&1 && continue
 elif command -v opkg >/dev/null 2>&1; then
  opkg status "$package" 2>/dev/null | grep -q 'Status:.* installed' && continue
 fi
 printf '%s\n' "$package"
done
""".replace("WORDS", words)
    output = transport._run_stage("installed package preflight", script)
    missing = output.decode().splitlines()
    if any(package not in packages for package in missing):
        from openwrt import DeployError
        raise DeployError("invalid package inventory response")
    return sorted(set(missing))
