"""Windows filesystem timestamps (100 ns precision) and timestamp policies.

Filesystem times are separate from PDF-internal metadata dates and from dates printed on pages;
nothing here touches document contents or the system clock.
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

EPOCH_AS_FILETIME = 116444736000000000  # 1601-01-01 -> 1970-01-01 in 100 ns ticks

SOURCES = ("download_modified", "download_created", "detected", "processing", "fixed")
POLICY_KINDS = ("download", "processing", "preserve", "custom")


def ns_to_filetime(ns: int) -> int:
    return ns // 100 + EPOCH_AS_FILETIME


def filetime_to_ns(ft: int) -> int:
    return (ft - EPOCH_AS_FILETIME) * 100


def ns_to_iso(ns: int | None) -> str | None:
    if ns is None:
        return None
    dt = datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)
    return dt.isoformat(timespec="microseconds")


def iso_to_ns(s: str) -> int:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:  # naive values are interpreted as local time
        dt = dt.astimezone()
    return int(dt.timestamp() * 1_000_000) * 1000


def ns_to_local_display(ns: int | None) -> str:
    if ns is None:
        return "—"
    return datetime.fromtimestamp(ns / 1e9).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


@dataclass
class FileTimes:
    created_ns: int
    accessed_ns: int
    modified_ns: int

    def as_dict(self):
        return {"created_ns": self.created_ns, "modified_ns": self.modified_ns, "accessed_ns": self.accessed_ns,
                "created": ns_to_iso(self.created_ns), "modified": ns_to_iso(self.modified_ns)}


if sys.platform == "win32":
    from ctypes import wintypes

    class _FILETIME(ctypes.Structure):
        _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.SetFileTime.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    _k32.GetFileTime.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD]
    _INVALID = wintypes.HANDLE(-1).value
    FILE_READ_ATTRIBUTES, FILE_WRITE_ATTRIBUTES = 0x80, 0x100
    SHARE_ALL = 0x1 | 0x2 | 0x4
    OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS = 3, 0x02000000

    def _open(path, access):
        h = _k32.CreateFileW(str(path), access, SHARE_ALL, None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
        if h == _INVALID or h is None:
            raise ctypes.WinError(ctypes.get_last_error())
        return h

    def _ft(ns):
        if ns is None:
            return None
        v = ns_to_filetime(ns)
        return _FILETIME(v & 0xFFFFFFFF, v >> 32)

    def get_times(path) -> FileTimes:
        h = _open(path, FILE_READ_ATTRIBUTES)
        try:
            c, a, m = _FILETIME(), _FILETIME(), _FILETIME()
            if not _k32.GetFileTime(h, ctypes.byref(c), ctypes.byref(a), ctypes.byref(m)):
                raise ctypes.WinError(ctypes.get_last_error())
            conv = lambda f: filetime_to_ns((f.hi << 32) | f.lo)  # noqa: E731
            return FileTimes(conv(c), conv(a), conv(m))
        finally:
            _k32.CloseHandle(h)

    def set_times(path, created_ns=None, modified_ns=None, accessed_ns=None) -> None:
        h = _open(path, FILE_WRITE_ATTRIBUTES)
        try:
            c, a, m = _ft(created_ns), _ft(accessed_ns), _ft(modified_ns)
            ok = _k32.SetFileTime(h, ctypes.byref(c) if c else None, ctypes.byref(a) if a else None,
                                  ctypes.byref(m) if m else None)
            if not ok:
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _k32.CloseHandle(h)

    def filesystem_name(path) -> str:
        root = os.path.splitdrive(os.path.abspath(str(path)))[0] + "\\"
        buf = ctypes.create_unicode_buffer(64)
        if _k32.GetVolumeInformationW(root, None, 0, None, None, None, buf, 64):
            return buf.value
        return "unknown"
else:  # pragma: no cover - development fallback only
    def get_times(path) -> FileTimes:
        st = os.stat(path)
        return FileTimes(getattr(st, "st_birthtime_ns", st.st_ctime_ns), st.st_atime_ns, st.st_mtime_ns)

    def set_times(path, created_ns=None, modified_ns=None, accessed_ns=None) -> None:
        st = os.stat(path)
        os.utime(path, ns=(accessed_ns or st.st_atime_ns, modified_ns or st.st_mtime_ns))

    def filesystem_name(path) -> str:
        return "unknown"


def tolerance_ns(path) -> int:
    fs = filesystem_name(path).upper()
    return 0 if fs in ("NTFS", "REFS") else 2_000_000_000


def _truncate(ns: int) -> int:
    return (ns // 100) * 100  # FILETIME resolution


@dataclass
class TimestampPolicy:
    kind: str = "download"
    created: str = "processing"           # custom only
    modified: str = "download_modified"   # custom only
    created_fixed: str | None = None      # ISO 8601 when created == "fixed"
    modified_fixed: str | None = None

    @classmethod
    def from_dict(cls, d: dict | None) -> "TimestampPolicy":
        d = dict(d or {})
        p = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        p.validate()
        return p

    def to_dict(self):
        return asdict(self)

    def validate(self):
        if self.kind not in POLICY_KINDS:
            raise ValueError(f"Unknown timestamp policy {self.kind!r}")
        if self.kind == "custom":
            for src, fixed in ((self.created, self.created_fixed), (self.modified, self.modified_fixed)):
                if src not in SOURCES:
                    raise ValueError(f"Unknown timestamp source {src!r}")
                if src == "fixed":
                    if not fixed:
                        raise ValueError("A fixed timestamp needs a value")
                    iso_to_ns(fixed)

    def describe(self) -> str:
        return {
            "download": "A — Modified = download time; Created = processing time",
            "processing": "B — Created and Modified = processing time",
            "preserve": "C — Preserve the downloaded file's timestamps",
            "custom": f"D — Custom (created: {self.created}, modified: {self.modified})",
        }[self.kind]


@dataclass
class TimestampContext:
    download_created_ns: int
    download_modified_ns: int
    detected_ns: int
    processing_ns: int = field(default_factory=time.time_ns)


def resolve_policy(policy: TimestampPolicy, ctx: TimestampContext) -> tuple[int, int]:
    """Return (created_ns, modified_ns) for the output."""
    def pick(src, fixed):
        return {
            "download_modified": ctx.download_modified_ns,
            "download_created": ctx.download_created_ns,
            "detected": ctx.detected_ns,
            "processing": ctx.processing_ns,
        }.get(src) if src != "fixed" else iso_to_ns(fixed)

    if policy.kind == "download":
        c, m = ctx.processing_ns, ctx.download_modified_ns
    elif policy.kind == "processing":
        c = m = ctx.processing_ns
    elif policy.kind == "preserve":
        c, m = ctx.download_created_ns, ctx.download_modified_ns
    else:
        c, m = pick(policy.created, policy.created_fixed), pick(policy.modified, policy.modified_fixed)
    return _truncate(c), _truncate(m)


def apply_and_verify(path, created_ns: int, modified_ns: int) -> dict:
    """Set times on the published file, read them back and report verification."""
    set_times(path, created_ns=created_ns, modified_ns=modified_ns, accessed_ns=_truncate(time.time_ns()))
    actual = get_times(path)
    tol = tolerance_ns(path)
    ok = abs(actual.created_ns - created_ns) <= tol and abs(actual.modified_ns - modified_ns) <= tol
    return {
        "requested": {"created_ns": created_ns, "modified_ns": modified_ns,
                      "created": ns_to_iso(created_ns), "modified": ns_to_iso(modified_ns)},
        "actual": actual.as_dict(),
        "tolerance_ns": tol,
        "filesystem": filesystem_name(path),
        "verified": ok,
    }
