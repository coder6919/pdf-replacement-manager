"""Download-folder monitoring with completion detection and a persistent, deduplicated job queue."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from .fsutil import can_open_exclusive, is_temp_download, read_zone_identifier, sha256_file, strip_url
from .logsetup import get_logger
from .pipeline import MAX_ATTEMPTS, Processor
from .repository import Repository
from .timestamps import get_times

log = get_logger("watcher")
RETRYABLE = ("file_locked", "disk_space")


def default_downloads_dir() -> Path:
    """The user's Downloads known folder (honours relocation), falling back to ~/Downloads."""
    try:
        import ctypes
        from ctypes import wintypes
        import uuid as _uuid

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]
        u = _uuid.UUID("{374DE290-123F-4565-9164-39C4925E467B}")
        g = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        p = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p)) == 0:
            path = p.value
            ctypes.windll.ole32.CoTaskMemFree(p)
            return Path(path)
    except Exception:
        pass
    return Path.home() / "Downloads"


class Pending:
    __slots__ = ("path", "first_seen", "last_sig", "last_change")

    def __init__(self, path: Path):
        self.path = path
        self.first_seen = time.monotonic()
        self.last_sig = None
        self.last_change = time.monotonic()


class DownloadWatcher:
    def __init__(self, repo: Repository, processor: Processor, folders: list[Path], *,
                 stable_seconds: float = 2.0, poll_interval: float = 0.5, max_wait: float = 6 * 3600,
                 rescan_interval: float = 30.0, use_observer: bool = True, hints=None, on_change=None):
        self.repo, self.processor = repo, processor
        self.folders = [Path(f) for f in folders]
        self.stable_seconds, self.poll_interval, self.max_wait = stable_seconds, poll_interval, max_wait
        self.rescan_interval = rescan_interval
        self.use_observer = use_observer
        self.hints = hints            # optional callable(file_name) -> {"url","referrer"} from the extension
        self.on_change = on_change or (lambda: None)
        self._pending: dict[str, Pending] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self._observer = None
        self._seen_on_scan: dict[str, tuple] = {}
        self.busy = False

    # ------------------------------------------------------------------ lifecycle
    def start(self, scan_since_ns: int | None = None):
        self._stop.clear()
        if self.use_observer:
            self._start_observer()
        if scan_since_ns is not None:
            self.scan(since_ns=scan_since_ns)
        else:
            self.scan(prime_only=True)
        for target, name in ((self._stabilize_loop, "prm-stabilize"), (self._worker_loop, "prm-worker")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self, timeout: float = 30):
        """Graceful stop: the worker finishes the job in progress (never killed mid-publish)."""
        self._stop.set()
        self._wake.set()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads.clear()

    def _start_observer(self):
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            log.warning("watchdog unavailable; relying on periodic rescans")
            return
        watcher = self

        class Handler(FileSystemEventHandler):
            def on_created(self, e):
                if not e.is_directory:
                    watcher.candidate(e.src_path)

            def on_modified(self, e):
                if not e.is_directory:
                    watcher.candidate(e.src_path)

            def on_moved(self, e):  # Chrome: 'x.pdf.crdownload' -> 'x.pdf'
                if not e.is_directory:
                    watcher.candidate(e.dest_path)

        obs = Observer()
        for f in self.folders:
            if f.is_dir():
                obs.schedule(Handler(), str(f), recursive=False)
        obs.daemon = True
        obs.start()
        self._observer = obs

    # ------------------------------------------------------------------ detection
    def candidate(self, path) -> bool:
        p = Path(path)
        if p.suffix.lower() != ".pdf" or is_temp_download(p):
            return False
        key = str(p.resolve()).lower() if p.exists() else str(p).lower()
        if key in self.processor.publishing:
            return False
        with self._lock:
            if key not in self._pending:
                self._pending[key] = Pending(p)
        return True

    def scan(self, since_ns: int | None = None, prime_only: bool = False):
        """Fallback/startup scan. prime_only records the current state without queueing anything."""
        for folder in self.folders:
            if not folder.is_dir():
                continue
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for e in entries:
                if not e.is_file() or not e.name.lower().endswith(".pdf"):
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                sig = (st.st_size, st.st_mtime_ns)
                key = e.path.lower()
                prev = self._seen_on_scan.get(key)
                self._seen_on_scan[key] = sig
                if prime_only:
                    continue
                if since_ns is not None:
                    if st.st_mtime_ns >= since_ns:
                        self.candidate(e.path)
                elif prev != sig:      # new or changed since the previous scan (missed watcher event)
                    self.candidate(e.path)

    def _stabilize_loop(self):
        last_scan = time.monotonic()
        last_retry = time.monotonic()
        while not self._stop.is_set():
            self.stabilize_once()
            now = time.monotonic()
            if now - last_scan >= self.rescan_interval:
                self.scan()
                last_scan = now
            if now - last_retry >= 60:
                self.requeue_retryable()
                last_retry = now
            self._stop.wait(self.poll_interval)

    def stabilize_once(self, force_now: bool = False) -> list[int]:
        created = []
        with self._lock:
            items = list(self._pending.items())
        for key, pend in items:
            p = pend.path
            done, reason = False, None
            try:
                if not p.exists():
                    done, reason = True, "vanished"
                elif Path(str(p) + ".crdownload").exists():
                    pend.last_change = time.monotonic()
                else:
                    st = p.stat()
                    sig = (st.st_size, st.st_mtime_ns)
                    if sig != pend.last_sig:
                        pend.last_sig, pend.last_change = sig, time.monotonic()
                    elif st.st_size > 0 and (force_now or time.monotonic() - pend.last_change >= self.stable_seconds):
                        if can_open_exclusive(p):
                            jid = self._finalize(p, st)
                            if jid:
                                created.append(jid)
                            done = True
                if not done and time.monotonic() - pend.first_seen > self.max_wait:
                    done, reason = True, "never completed"
                    log.warning("download never stabilised", extra={"input_path": str(p)})
            except OSError:
                pass
            if done:
                with self._lock:
                    self._pending.pop(key, None)
        if created:
            self._wake.set()
            self.on_change()
        return created

    def _finalize(self, p: Path, st) -> int | None:
        times = get_times(p)              # record the download's own timestamps before anything touches it
        detected = time.time_ns()
        sha = sha256_file(p)
        if self.repo.is_our_output(str(p), sha):
            return None                   # our own published output (loop prevention)
        zone = read_zone_identifier(p)
        url, ref = zone.get("HostUrl"), zone.get("ReferrerUrl")
        if (not url or url.startswith(("about:", "blob:"))) and self.hints:
            h = self.hints(p.name) or {}
            url, ref = h.get("url") or url, h.get("referrer") or ref
        url, ref = strip_url(url), strip_url(ref)
        key = f"{str(p.resolve()).lower()}|{st.st_size}|{times.modified_ns}"
        jid = self.repo.create_job(dedupe_key=key, source_path=str(p.resolve()), file_name=p.name, size=st.st_size,
                                   created_ns=times.created_ns, modified_ns=times.modified_ns, detected_ns=detected,
                                   source_url=url, referrer_url=ref, zone_raw=zone.get("_raw"), sha256=sha)
        if jid:
            log.info("download detected", extra={"job_id": jid, "input_path": str(p), "detected_ns": detected})
        return jid

    # ------------------------------------------------------------------ worker
    def _worker_loop(self):
        while not self._stop.is_set():
            if not self.run_next():
                self._wake.wait(2.0)
                self._wake.clear()

    def run_next(self) -> bool:
        job = self.repo.next_queued_job()
        if not job:
            return False
        self.busy = True
        try:
            self.processor.process(job["id"])
        finally:
            self.busy = False
            self.on_change()
        return True

    def requeue_retryable(self):
        for job in self.repo.jobs(("failed",), limit=50):
            if job["error_code"] in RETRYABLE and job["attempts"] < MAX_ATTEMPTS:
                self.repo.update_job(job["id"], state="queued")
                self.repo.event(job["id"], "info", "Automatic retry (temporary problem: "
                                                   f"{job['error_code'].replace('_', ' ')})")
                self._wake.set()

    def wake(self):
        self._wake.set()

    def pending_paths(self) -> list[str]:
        with self._lock:
            return [str(p.path) for p in self._pending.values()]
