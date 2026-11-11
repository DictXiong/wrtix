"""Human-readable resource comparisons; never part of the JSON protocol."""
import base64
import difflib
import hashlib
import sys

from progress import Progress


class DiffError(ValueError):
    pass


def decode_contents(snapshot, contents):
    """Bind display bytes to the same immutable baseline used for staging."""
    result = {}
    for path, encoded in contents.items():
        try:
            data = base64.b64decode(encoded, validate=True)
        except (TypeError, ValueError):
            raise DiffError("invalid diff content encoding") from None
        if len(data) > 1048576 or hashlib.sha256(data).hexdigest() != snapshot["files"].get(path):
            raise DiffError("configuration changed while reading diff; retry")
        result[path] = data
    return result


def safe_text(value):
    # Configuration may contain terminal escape sequences. Display them literally.
    return "".join(c if c in "\n\t" or c.isprintable() else "\\x%02x" % ord(c) for c in str(value))


def text_lines(data):
    value = data.decode("utf-8")
    if "\0" in value:
        raise UnicodeError()
    return value.splitlines(keepends=True)


def render_diff(device, current, target, before, after, *, opaque=(), label="desired", root_sync=False):
    rows = [f"[{device}] configuration diff (current -> {label})\n"]
    hidden = set(opaque)
    changed = False
    for path in sorted(target["files"]):
        old_hash, new_hash = current["files"].get(path), target["files"][path]
        old_mode, new_mode = current["modes"].get(path), target["modes"].get(path)
        if (old_hash, old_mode) == (new_hash, new_mode):
            continue
        changed = True
        rows.append(f"  /{path}: " + ("create" if old_hash is None else "remove" if new_hash is None else "change") + "\n")
        if old_mode != new_mode:
            rows.append(f"    mode: {old_mode or 'absent'} -> {new_mode or 'absent'}\n")
        if old_hash == new_hash:
            continue
        if path in hidden:
            rows.append("    content hidden (secret-derived or private system resource)\n")
            continue
        if ((old_hash is not None and path not in before)
                or (new_hash is not None and path not in after)):
            raise DiffError("diff content unavailable; operation stopped")
        try:
            left, right = text_lines(before.get(path, b"")), text_lines(after.get(path, b""))
        except UnicodeError:
            rows.append("    binary content differs\n")
            continue
        lines = difflib.unified_diff(left, right, fromfile=f"current/{path}" if old_hash else "/dev/null",
                                    tofile=f"{label}/{path}" if new_hash else "/dev/null")
        for index, line in enumerate(lines):
            if index == 2000:
                rows.append("    [diff truncated after 2000 lines for this file]\n")
                break
            rows.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    for name, desired in sorted(target.get("services", {}).items()):
        actual = current.get("services", {}).get(name, {})
        for key in ("present", "enabled", "running"):
            expected = desired.get(key)
            if expected is not None and actual.get(key) != expected:
                changed = True
                rows.append(f"  service {name} {key}: {actual.get(key)} -> {expected}\n")
    if target.get("resolver_state") is not None and target["resolver_state"] != current.get("resolver_state"):
        changed = True
        def resolver(value):
            value = value or {}
            return ("symlink " + value["target"] if value.get("type") == "symlink"
                    else value.get("type", "unknown") + (" mode " + value["mode"] if value.get("mode") else ""))
        rows.append(f"  /etc/resolv.conf: {resolver(current.get('resolver_state'))} -> {resolver(target['resolver_state'])}\n")
    if root_sync:
        changed = True
        rows.append("  root password: synchronization required (content hidden)\n")
    if not changed:
        rows.append("  no differences\n")
    return safe_text("".join(rows))


def show_diff(*args, **kwargs):
    output = render_diff(*args, **kwargs)
    with Progress.external_output():
        sys.stderr.write(output)
        sys.stderr.flush()
