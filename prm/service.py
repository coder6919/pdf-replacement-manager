"""Application service: composes storage, processing, monitoring and the optional Chrome bridge."""
from __future__ import annotations

import secrets
import sys
import threading
import time
from pathlib import Path

from .db import Database
from .fsutil import is_temp_download, read_zone_identifier, sha256_file, strip_url
from .logsetup import get_logger, setup_logging
from .matching import evaluate, match
from .paths import AppPaths
from .pdfinfo import content_fingerprint, inspect_pdf, open_pdf, similarity
from .pipeline import Processor
from .repository import Repository
from .timestamps import get_times
from .watcher import DownloadWatcher, default_downloads_dir

log = get_logger("service")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "PDFReplacementManager"


def default_settings() -> dict:
    return {
        "watch_folders": [str(default_downloads_dir())],
        "monitoring_enabled": True,
        "stable_seconds": 2.0,
        "original_retention_days": 90,
        "global_dry_run": False,
        "bridge_enabled": False,
        "bridge_port": 47821,
        "bridge_token": secrets.token_urlsafe(24),
        "start_with_windows": False,
        "minimize_to_tray": True,
        "notifications": True,
    }


class AppService:
    def __init__(self, data_dir: Path | str | None = None, *, use_observer: bool = True):
        self.paths = AppPaths(data_dir).ensure()
        setup_logging(self.paths.logs)
        self.db = Database(self.paths.db)
        self.repo = Repository(self.db, self.paths)
        self.processor = Processor(self.repo, notifier=self._notify)
        self.use_observer = use_observer
        self.watcher: DownloadWatcher | None = None
        self.bridge = None
        self._listeners: list = []
        self._change_listeners: list = []
        self._hb_stop = threading.Event()
        self._hb_thread = None
        s = self.settings()
        if self.db.get_setting("bridge_token") is None:
            self.db.set_setting("bridge_token", s["bridge_token"])

    # ------------------------------------------------------------------ settings
    def settings(self) -> dict:
        out = default_settings()
        for k in list(out):
            v = self.db.get_setting(k)
            if v is not None:
                out[k] = v
        return out

    def set_settings(self, **kw):
        for k, v in kw.items():
            self.db.set_setting(k, v)
        if "start_with_windows" in kw:
            set_autostart(bool(kw["start_with_windows"]))

    # ------------------------------------------------------------------ events
    def add_notification_listener(self, fn):
        self._listeners.append(fn)

    def add_change_listener(self, fn):
        self._change_listeners.append(fn)

    def _notify(self, job, kind, message):
        for fn in list(self._listeners):
            try:
                fn(job, kind, message)
            except Exception:
                pass

    def _changed(self):
        for fn in list(self._change_listeners):
            try:
                fn()
            except Exception:
                pass

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        recovered = self.processor.recover()
        if recovered:
            log.info("recovered interrupted jobs", extra={"recovery": recovered})
        try:
            self.repo.purge_originals(int(self.settings()["original_retention_days"]))
        except Exception:
            log.exception("retention purge failed")
        self.start_monitoring()
        self._hb_stop.clear()
        self._hb_thread = threading.Thread(target=self._heartbeat, name="prm-heartbeat", daemon=True)
        self._hb_thread.start()
        if self.settings()["bridge_enabled"]:
            self.start_bridge()

    def start_monitoring(self):
        s = self.settings()
        if self.watcher:
            self.watcher.stop()
        folders = [Path(f) for f in s["watch_folders"] if f]
        self.watcher = DownloadWatcher(self.repo, self.processor, folders, stable_seconds=float(s["stable_seconds"]),
                                       use_observer=self.use_observer, hints=self._hint_for,
                                       on_change=self._changed)
        last = self.db.get_setting("last_running_ns")
        if s["monitoring_enabled"]:
            # downloads that arrived while the app was not running are picked up (since last heartbeat)
            self.watcher.start(scan_since_ns=last)
        self._heartbeat_once()

    def stop(self):
        self._hb_stop.set()
        if self.watcher:
            self.watcher.stop()
        self.stop_bridge()
        self._heartbeat_once()

    def _heartbeat_once(self):
        self.db.set_setting("last_running_ns", time.time_ns())

    def _heartbeat(self):
        while not self._hb_stop.wait(30):
            try:
                self._heartbeat_once()
            except Exception:
                pass

    @property
    def monitoring(self) -> bool:
        return bool(self.watcher and self.watcher._threads)

    # ------------------------------------------------------------------ bridge (optional Chrome extension)
    def start_bridge(self):
        from .bridge import Bridge
        s = self.settings()
        self.stop_bridge()
        self.bridge = Bridge(int(s["bridge_port"]), self.db.get_setting("bridge_token"), self.repo)
        self.bridge.start()

    def stop_bridge(self):
        if self.bridge:
            self.bridge.stop()
            self.bridge = None

    def _hint_for(self, file_name):
        return self.bridge.hint_for(file_name) if self.bridge else None

    # ------------------------------------------------------------------ manual processing & diagnostics
    def submit_file(self, path: Path | str, *, profile_id: int | None = None, dry_run: bool = False,
                    force: bool = False, process_now: bool = True) -> int:
        """One-time manual processing of a chosen PDF (same transactional pipeline)."""
        p = Path(path).resolve()
        times = get_times(p)
        zone = read_zone_identifier(p)
        jid = self.repo.create_job(
            dedupe_key=f"manual|{p}|{time.time_ns()}", source_path=str(p), file_name=p.name, size=p.stat().st_size,
            created_ns=times.created_ns, modified_ns=times.modified_ns, detected_ns=time.time_ns(),
            source_url=strip_url(zone.get("HostUrl")), referrer_url=strip_url(zone.get("ReferrerUrl")),
            zone_raw=zone.get("_raw"),
            dry_run=dry_run or self.settings()["global_dry_run"], force=force or profile_id is not None,
            profile_id=profile_id)
        self.repo.event(jid, "info", "Submitted manually")
        if process_now:
            self.processor.process(jid)
            self._changed()
        elif self.watcher:
            self.watcher.wake()
        return jid

    def diagnose_file(self, path: Path | str) -> dict:
        """Explain whether and why a file would be processed — without changing anything."""
        p = Path(path)
        out = {"path": str(p), "exists": p.is_file(), "steps": []}
        add = out["steps"].append
        if not p.is_file():
            add(("File", "does not exist"))
            return out
        if p.suffix.lower() != ".pdf":
            add(("Type", "not a .pdf file — ignored"))
        if is_temp_download(p):
            add(("Name", "temporary/partial download name — ignored until complete"))
        folders = [Path(f).resolve() for f in self.settings()["watch_folders"]]
        add(("Folder", "inside a watched folder" if p.resolve().parent in folders
             else "NOT in a watched folder (automatic processing will not see it)"))
        sha = sha256_file(p)
        if self.repo.is_our_output(str(p), sha):
            add(("Origin", "this file is an output this app published — ignored (loop prevention)"))
        zone = read_zone_identifier(p)
        add(("Source", zone.get("HostUrl") or "unknown (no Zone.Identifier stream)"))
        info = inspect_pdf(p)
        add(("PDF", "unreadable: " + (info.error or "") if not info.ok else
             f"{info.page_count} pages; " + ("; ".join(info.warnings) or "no warnings")))
        fp = None
        if info.ok and not info.needs_password:
            d = open_pdf(p)
            try:
                fp = content_fingerprint(d)
            finally:
                d.close()
        for prof in self.repo.list_profiles():
            score, reason = evaluate(prof, p.name, sha, zone.get("HostUrl"))
            line = ("enabled" if prof.enabled else "DISABLED") + "; " + (f"rules match ({reason})" if score
                                                                       else f"no match: {reason}")
            if score and prof.reference_fingerprint and fp:
                line += f"; content similarity {similarity(fp, prof.reference_fingerprint):.0%} " \
                        f"(threshold {float(prof.rules.get('similarity_threshold', 0.55)):.0%})"
            add((f"Profile '{prof.name}'", line))
        m = match(self.repo.list_profiles(), p.name, sha, zone.get("HostUrl"), fp)
        out["decision"], out["reason"] = m.decision, m.reason
        add(("Decision", f"{m.decision}: {m.reason}"))
        return out


def set_autostart(enabled: bool) -> None:
    """Per-user autostart (HKCU Run key) — no administrator rights needed."""
    if sys.platform != "win32":
        return
    import winreg
    exe = sys.executable if getattr(sys, "frozen", False) else None
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled and exe:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, f'"{exe}" --minimized')
        elif enabled:
            pyw = Path(sys.executable).with_name("pythonw.exe")
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, f'"{pyw}" -m prm --minimized')
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass
