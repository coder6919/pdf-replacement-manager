"""Safe filesystem primitives: sanitising, hashing, verified copies, atomic publish, lock checks."""
from __future__ import annotations

import ctypes
import hashlib
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

from .errors import FileLockedError, InsufficientSpaceError, IntegrityError

_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
TEMP_SUFFIXES = (".crdownload", ".tmp", ".part", ".partial", ".download")
COLLISION_RE = re.compile(r"^(?P<stem>.*?) \((?P<n>\d+)\)$")
MAX_NAME = 180


def sanitize_filename(name: str, default: str = "document.pdf") -> str:
    """Return a safe single path component (no traversal, reserved names, or control chars)."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = _BAD_CHARS.sub("_", name).strip().rstrip(". ")
    if not name or name in {".", ".."}:
        name = default
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    if stem.split(".")[0].upper() in _RESERVED:
        stem = "_" + stem
    if len(stem) + len(ext) + 1 > MAX_NAME:
        stem = stem[: MAX_NAME - len(ext) - 1]
    return f"{stem}.{ext}" if ext else stem


def normalize_collision_name(name: str) -> str:
    """'Report (2).pdf' -> 'Report.pdf' (Chrome renames on collision)."""
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    m = COLLISION_RE.match(stem)
    if m:
        stem = m.group("stem")
    return f"{stem}.{ext}" if ext else stem


def is_temp_download(path: Path | str) -> bool:
    n = str(path).lower()
    base = os.path.basename(n)
    return n.endswith(TEMP_SUFFIXES) or base.startswith("~prm-") or base.startswith("~$")


def sha256_file(path: Path | str, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ensure_free_space(directory: Path | str, needed: int, margin: int = 50 * 1024 * 1024) -> None:
    d = Path(directory)
    while not d.exists() and d.parent != d:
        d = d.parent
    free = shutil.disk_usage(d).free
    if free < needed + margin:
        raise InsufficientSpaceError(
            f"Not enough free disk space on {d.anchor or d}: need {(needed + margin) // 1048576} MB, "
            f"have {free // 1048576} MB. Nothing was changed.")


def _fsync_file(path: Path | str) -> None:
    fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def copy_verified(src: Path | str, dst: Path | str, expected_sha: str | None = None) -> str:
    """Copy src to dst via a temp file, fsync, verify SHA-256, then atomically rename.

    Uses shutil.copyfile (content only) so source timestamps are NOT carried over.
    """
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    expected = expected_sha or sha256_file(src)
    tmp = dst.with_name(f"~prm-{uuid.uuid4().hex[:8]}.tmp")
    try:
        shutil.copyfile(src, tmp)
        _fsync_file(tmp)
        actual = sha256_file(tmp)
        if actual != expected:
            raise IntegrityError(f"Copy verification failed for {dst.name} (hash mismatch).")
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return expected


def write_atomic(path: Path | str, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"~prm-{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------- Windows specifics
if sys.platform == "win32":
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    INVALID_HANDLE = wintypes.HANDLE(-1).value
    GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
    FILE_SHARE_READ = 0x1
    OPEN_EXISTING = 3
    ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION = 32, 33


def can_open_exclusive(path: Path | str, write: bool = False) -> bool:
    """True if nobody else holds a write handle (read check) or any handle (write check)."""
    if sys.platform != "win32":
        try:
            with open(path, "rb"):
                return True
        except OSError:
            return False
    access = GENERIC_READ | (GENERIC_WRITE if write else 0)
    share = 0 if write else FILE_SHARE_READ
    h = _k32.CreateFileW(str(path), access, share, None, OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE or h is None:
        return False
    _k32.CloseHandle(h)
    return True


def lock_error_code(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) in (32, 33) or isinstance(exc, PermissionError)


def read_zone_identifier(path: Path | str) -> dict:
    """Read Chrome's Mark-of-the-Web (Zone.Identifier ADS): ZoneId, HostUrl, ReferrerUrl."""
    if sys.platform != "win32":
        return {}
    try:
        with open(f"{path}:Zone.Identifier", "r", encoding="utf-8", errors="replace") as f:
            text = f.read(8192)
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip() in ("ZoneId", "HostUrl", "ReferrerUrl"):
            out[k.strip()] = v.strip()
    out["_raw"] = text
    return out


def strip_url(url: str | None) -> str | None:
    """Keep scheme://host/path only: query strings of signed download links can carry temporary tokens."""
    if not url:
        return url
    from urllib.parse import urlsplit, urlunsplit
    try:
        u = urlsplit(url)
    except ValueError:
        return None
    if u.scheme not in ("http", "https"):
        return f"{u.scheme}:" if u.scheme else None
    return urlunsplit((u.scheme, u.hostname or "", u.path, "", ""))


def write_zone_identifier(path: Path | str, raw: str) -> None:
    """Propagate Mark-of-the-Web onto a generated file so Windows protections still apply."""
    if sys.platform != "win32" or not raw:
        return
    try:
        with open(f"{path}:Zone.Identifier", "w", encoding="utf-8") as f:
            f.write(raw)
    except OSError:
        pass


def publish_atomic(staged: Path | str, target: Path | str, expected_sha: str, *,
                   zone_raw: str | None = None, retries: int = 5, delay: float = 1.0,
                   before_rename=None) -> None:
    """Copy staged file into the target's directory (same volume), verify, then os.replace.

    Cross-volume safety: the cross-volume part is a copy; the replace is always same-directory.
    Retries on sharing violations (e.g. target open in a PDF viewer), then raises FileLockedError.
    """
    staged, target = Path(staged), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"~prm-{uuid.uuid4().hex[:8]}.tmp")
    try:
        shutil.copyfile(staged, tmp)
        _fsync_file(tmp)
        if sha256_file(tmp) != expected_sha:
            raise IntegrityError("Staged output changed while publishing (hash mismatch).")
        if zone_raw:
            write_zone_identifier(tmp, zone_raw)
        if before_rename:
            before_rename(tmp)
        last = None
        for attempt in range(retries):
            try:
                os.replace(tmp, target)
                return
            except OSError as e:
                if not lock_error_code(e):
                    raise
                last = e
                time.sleep(delay * (attempt + 1))
        raise FileLockedError(
            f"'{target.name}' is locked by another program (for example a PDF viewer). "
            f"Close it and retry the job. The original is untouched.", detail=str(last))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(2, 10000):
        cand = path.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
    raise FileExistsError(path)


def open_with_default_app(path: Path | str) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - opens the user's own generated file


def find_chrome() -> str | None:
    cands = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    return next((c for c in cands if os.path.isfile(c)), None)


def open_in_chrome(path: Path | str) -> bool:
    import subprocess
    exe = find_chrome()
    if not exe:
        return False
    subprocess.Popen([exe, Path(path).resolve().as_uri()], close_fds=True)  # noqa: S603 - fixed exe, file URI arg
    return True
