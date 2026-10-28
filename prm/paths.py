"""Per-user application data layout."""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR_NAME = "PDFReplacementManager"


class AppPaths:
    """All managed locations live below one per-user root (default %LOCALAPPDATA%)."""

    def __init__(self, root: Path | str | None = None):
        if root is None:
            env = os.environ.get("PRM_DATA_DIR")
            if env:
                root = Path(env)
            else:
                base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
                root = Path(base) / DATA_DIR_NAME
        self.root = Path(root).resolve()
        self.db = self.root / "prm.sqlite3"
        self.masters = self.root / "masters"
        self.assets = self.root / "assets"
        self.originals = self.root / "originals"
        self.staging = self.root / "staging"
        self.sessions = self.root / "sessions"
        self.logs = self.root / "logs"
        self.backups = self.root / "backups"
        self.outputs = self.root / "outputs"

    def ensure(self) -> "AppPaths":
        for p in (self.root, self.masters, self.assets, self.originals, self.staging,
                  self.sessions, self.logs, self.backups, self.outputs):
            p.mkdir(parents=True, exist_ok=True)
        return self

    def rel(self, path: Path | str) -> str:
        """Path relative to the data root, stored in the DB (portable across backup/restore)."""
        p = Path(path).resolve()
        return p.relative_to(self.root).as_posix()

    def resolve(self, rel: str) -> Path:
        """Resolve a stored relative path, refusing anything that escapes the data root."""
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError(f"Managed path escapes data directory: {rel!r}")
        return p

    def is_managed(self, path: Path | str) -> bool:
        p = Path(path).resolve()
        return p == self.root or self.root in p.parents
