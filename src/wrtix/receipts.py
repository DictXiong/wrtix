"""Private local transaction state; management never contacts Nix, SOPS or an AP."""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time


class ReceiptError(ValueError):
    pass


_SUFFIX = ".receipt.json"
_DEVICE = re.compile(r"[A-Za-z0-9_]{1,64}")
_HOST = re.compile(r"[A-Za-z0-9_.:%\[\]-]+")
_TERMINAL = frozenset(("confirmed", "rolled-back"))
_STATES = _TERMINAL | frozenset(("prepared", "staged", "applying", "applied", "rollback_pending", "rollback_incomplete", "reload_failed"))
_MAXIMUM = 2 * 1024 * 1024
_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def default_directory():
    configured = os.environ.get("XDG_STATE_HOME", "")
    base = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".local/state"
    return Path(os.path.abspath(base)) / "openwrt-rebuild/receipts"


def _device(value):
    if value is not None and (not isinstance(value, str) or not _DEVICE.fullmatch(value)):
        raise ReceiptError("invalid receipt device identifier")


def _open_directory(create):
    """Walk without following symlinks; never chmod an existing user directory."""
    target = default_directory()
    descriptor = None
    try:
        descriptor = os.open("/", _FLAGS)
        for index, part in enumerate(target.parts[1:], 1):
            try:
                child = os.open(part, _FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    return None
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, _FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            private = index >= len(target.parts) - 2
            mode = stat.S_IMODE(info.st_mode)
            if info.st_uid not in (0, os.geteuid()):
                raise ReceiptError("receipt state directory has an unexpected owner")
            if private and (info.st_uid != os.geteuid() or mode != 0o700):
                raise ReceiptError("receipt state directory must be private with mode 0700")
            if mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
                raise ReceiptError("receipt state directory has a writable ancestor")
        result, descriptor = descriptor, None
        return result
    except OSError:
        raise ReceiptError("receipt state directory is unavailable or unsafe") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextlib.contextmanager
def receipt_lock(path=None):
    """Serialize managed writes and cleanup; explicitly external files are untouched."""
    if path is not None and Path(os.path.abspath(path)).parent != default_directory():
        yield None
        return
    directory = _open_directory(create=path is not None)
    if directory is None:
        yield None
        return
    lock = None
    try:
        try:
            lock = os.open(".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
            info = os.fstat(lock)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise ReceiptError("receipt state lock is unsafe")
            fcntl.flock(lock, fcntl.LOCK_EX)
        except OSError:
            raise ReceiptError("receipt state lock is unavailable or unsafe") from None
        yield directory
    finally:
        if lock is not None:
            os.close(lock)
        os.close(directory)


def allocate_receipt(device):
    """Choose an unused name; the caller must publish with atomic no-replace semantics."""
    _device(device)
    if device is None:
        raise ReceiptError("a receipt requires a device identifier")
    directory = default_directory()
    # Supplying a managed child path requests creation; no empty receipt is made.
    with receipt_lock(directory / "allocation") as descriptor:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for _ in range(20):
            name = device + "-" + stamp + "-" + secrets.token_hex(8) + _SUFFIX
            try:
                os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return directory / name
    raise ReceiptError("could not allocate a unique receipt name")


def _unique(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read(directory, name):
    descriptor = None
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1 or before.st_size > _MAXIMUM):
            return None
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(_MAXIMUM + 1)
            after = os.fstat(stream.fileno())
        if len(raw) > _MAXIMUM or _identity(before) != _identity(after):
            return None
        data = json.loads(raw, object_pairs_hook=_unique)
        if (not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1
                or not isinstance(data.get("state"), str)
                or not isinstance(data.get("device_id"), str) or not _DEVICE.fullmatch(data["device_id"])
                or not isinstance(data.get("host"), str) or not _HOST.fullmatch(data["host"]) or data["host"].startswith("-")
                or type(data.get("port")) is not int or not 1 <= data["port"] <= 65535
                or not isinstance(data.get("token"), str) or not re.fullmatch(r"[0-9a-f]{32}", data["token"])):
            return None
        return data, after, hashlib.sha256(raw).digest()
    except (OSError, ValueError, UnicodeError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def list_receipts(device=None):
    _device(device)
    directory, result, now = default_directory(), [], time.time()
    with receipt_lock() as descriptor:
        if descriptor is None:
            return result
        for name in sorted(os.listdir(descriptor)):
            if not name.endswith(_SUFFIX):
                continue
            value = _read(descriptor, name)
            if value is None:
                continue
            data, info, _ = value
            if device is not None and data["device_id"] != device:
                continue
            try:
                updated = datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat()
            except (OverflowError, OSError, ValueError):
                continue
            result.append({"receipt": str(directory / name), "device_id": data["device_id"],
                           "host": data["host"], "port": data["port"],
                           "state": data.get("state") if data.get("state") in _STATES else "unknown",
                           "updated_at": updated, "age_days": max(0, int((now - info.st_mtime) // 86400))})
    return sorted(result, key=lambda entry: (entry["updated_at"], entry["receipt"]), reverse=True)


def _restore_claim(directory, claim, original):
    # A concurrent atomic writer may already have repopulated the original name.
    # Keep the claimed capability discoverable instead of overwriting that writer.
    try:
        os.link(claim, original, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
    except FileExistsError:
        return
    os.unlink(claim, dir_fd=directory)


def clean_receipts(device=None, older_than_days=30):
    _device(device)
    if type(older_than_days) is not int or older_than_days < 0:
        raise ReceiptError("receipt age must be a non-negative number of days")
    directory = default_directory()
    result = {"removed": [], "retained": 0, "skipped": 0}
    try:
        threshold = time.time() - older_than_days * 86400
    except OverflowError:
        raise ReceiptError("receipt age is out of range") from None
    with receipt_lock() as descriptor:
        if descriptor is None:
            return result
        for name in sorted(os.listdir(descriptor)):
            if not name.endswith(_SUFFIX):
                continue
            value = _read(descriptor, name)
            if value is None:
                result["skipped"] += 1
                continue
            data, info, digest = value
            if device is not None and data["device_id"] != device:
                continue
            if data.get("state") not in _TERMINAL or info.st_mtime >= threshold:
                result["retained"] += 1
                continue
            for _ in range(20):
                claim = ".cleanup-" + secrets.token_hex(16) + _SUFFIX
                try:
                    os.stat(claim, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    break
            else:
                raise ReceiptError("could not allocate a receipt cleanup name")
            try:
                os.rename(name, claim, src_dir_fd=descriptor, dst_dir_fd=descriptor)
                claimed = _read(descriptor, claim)
                if claimed is None or _identity(claimed[1]) != _identity(info) or claimed[2] != digest:
                    _restore_claim(descriptor, claim, name)
                    result["skipped"] += 1
                    continue
                os.unlink(claim, dir_fd=descriptor)
            except FileNotFoundError:
                result["skipped"] += 1
                continue
            except OSError:
                # Preserve any claimed receipt if the directory changed externally.
                raise ReceiptError("receipt cleanup could not safely finish") from None
            result["removed"].append(str(directory / name))
    return result
