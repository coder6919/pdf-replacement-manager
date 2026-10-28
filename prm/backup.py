"""Backup and restore of the application's data (database + managed files) as a verified ZIP."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import __version__
from .errors import IntegrityError, PRMError
from .fsutil import sha256_file

MANAGED_DIRS = ("masters", "assets", "originals")


def export_backup(service, dest: Path | str, include_originals: bool = True) -> dict:
    dest = Path(dest)
    paths = service.paths
    with tempfile.TemporaryDirectory(dir=paths.root) as td:
        snap = Path(td) / "prm.sqlite3"
        service.db.backup_to(snap)                          # consistent online snapshot
        files = [("prm.sqlite3", snap)]
        for d in MANAGED_DIRS:
            if d == "originals" and not include_originals:
                continue
            base = paths.root / d
            for f in sorted(base.rglob("*")):
                if f.is_file() and not f.name.startswith("~prm-"):
                    files.append((f.relative_to(paths.root).as_posix(), f))
        manifest = {"app_version": __version__, "created": datetime.now().astimezone().isoformat(),
                    "schema": service.db.q1("PRAGMA user_version")[0],
                    "files": [{"path": arc, "sha256": sha256_file(src), "size": src.stat().st_size}
                              for arc, src in files]}
        tmp_zip = dest.with_name(dest.name + ".partial")
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(manifest, indent=1))
            for arc, src in files:
                z.write(src, arc)
        verify_backup(tmp_zip)
        os.replace(tmp_zip, dest)
    return manifest


def _safe_member(name: str) -> bool:
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or ":" in name or "\\" in name:
        return False
    return name in ("manifest.json", "prm.sqlite3") or (len(p.parts) >= 2 and p.parts[0] in MANAGED_DIRS)


def verify_backup(zip_path: Path | str) -> dict:
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        if "manifest.json" not in names or "prm.sqlite3" not in names:
            raise IntegrityError("Not a PDF Replacement Manager backup (manifest or database missing).")
        bad = [n for n in names if not _safe_member(n)]
        if bad:
            raise IntegrityError(f"Backup contains unsafe paths and was rejected: {bad[:3]}")
        manifest = json.loads(z.read("manifest.json"))
        listed = {f["path"]: f for f in manifest["files"]}
        for n in names:
            if n == "manifest.json":
                continue
            if n not in listed:
                raise IntegrityError(f"Backup member not in manifest: {n}")
            import hashlib
            h = hashlib.sha256()
            with z.open(n) as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != listed[n]["sha256"]:
                raise IntegrityError(f"Backup member failed its checksum: {n}")
        return manifest


def restore_backup(service, zip_path: Path | str) -> dict:
    """Restore database + managed files. The current database is kept as a pre-restore copy.

    Managed files are merged (never deleted), so nothing currently on disk is lost.
    """
    manifest = verify_backup(zip_path)
    from .db import SCHEMA_VERSION
    if manifest.get("schema", 0) > SCHEMA_VERSION:
        raise PRMError("This backup was made by a newer version of the application.")
    paths = service.paths
    service.stop()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    pre = paths.backups / f"pre-restore-{stamp}.sqlite3"
    service.db.backup_to(pre)
    service.db.close()
    with tempfile.TemporaryDirectory(dir=paths.root) as td:
        with zipfile.ZipFile(zip_path) as z:
            for n in z.namelist():
                if n == "manifest.json":
                    continue
                target = (Path(td) / n).resolve()
                if Path(td).resolve() not in target.parents:
                    raise IntegrityError("Unsafe path in backup.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(n) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        restored = 0
        for f in manifest["files"]:
            if f["path"] == "prm.sqlite3":
                continue
            src = Path(td) / f["path"]
            dst = paths.resolve(f["path"])
            if dst.exists() and sha256_file(dst) == f["sha256"]:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():   # different content at same path: keep the current one aside
                os.replace(dst, dst.with_name(dst.name + f".pre-restore-{stamp}"))
            shutil.copyfile(src, dst)
            restored += 1
        for suffix in ("-wal", "-shm"):
            w = Path(str(paths.db) + suffix)
            if w.exists():
                for _ in range(10):
                    try:
                        w.unlink()
                        break
                    except PermissionError:
                        time.sleep(0.2)
        for _ in range(20):
            try:
                os.replace(Path(td) / "prm.sqlite3", paths.db)
                break
            except PermissionError:
                time.sleep(0.25)
        else:
            raise PRMError("Database is in use; close other windows and try again.")
    service.db.migrate()   # reconnects lazily; upgrades older backups
    return {"files_restored": restored, "pre_restore_copy": str(pre), "manifest_created": manifest["created"]}
