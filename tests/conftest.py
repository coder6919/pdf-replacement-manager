"""Shared fixtures. Every test runs in isolated temporary directories — never the real Downloads folder."""
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from prm import samples  # noqa: E402
from prm.repository import Profile  # noqa: E402
from prm.service import AppService  # noqa: E402
from prm.timestamps import get_times, set_times  # noqa: E402

DAY = 86400 * 10**9


@pytest.fixture(autouse=True)
def _guard_real_downloads(monkeypatch, tmp_path):
    """Make sure nothing can default to the user's real Downloads/AppData folders."""
    monkeypatch.setenv("PRM_DATA_DIR", str(tmp_path / "appdata-default"))
    import prm.watcher as w
    monkeypatch.setattr(w, "default_downloads_dir", lambda: tmp_path / "downloads-default")
    import prm.service as s
    monkeypatch.setattr(s, "default_downloads_dir", lambda: tmp_path / "downloads-default")


@pytest.fixture
def downloads(tmp_path) -> Path:
    d = tmp_path / "Downloads"
    d.mkdir()
    return d


@pytest.fixture
def service(tmp_path, downloads):
    svc = AppService(tmp_path / "appdata", use_observer=False)
    svc.set_settings(watch_folders=[str(downloads)], stable_seconds=0.2)
    svc.processor.opener = lambda p: None
    svc.processor.chrome_opener = lambda p: None
    yield svc
    svc.stop()
    svc.db.close()


def chrome_download(folder: Path, name: str, data: bytes, *, mtime_ns: int | None = None,
                    zone_host: str | None = None) -> Path:
    """Mimic Chrome: write '<name>.crdownload', then rename to the final name."""
    part = folder / f"{name}.crdownload"
    part.write_bytes(data)
    final = folder / name
    os.replace(part, final)
    if zone_host:
        with open(f"{final}:Zone.Identifier", "w") as f:
            f.write(f"[ZoneTransfer]\nZoneId=3\nReferrerUrl=https://{zone_host}/reports\n"
                    f"HostUrl=https://{zone_host}/files/{name}\n")
    if mtime_ns:
        set_times(final, created_ns=mtime_ns, modified_ns=mtime_ns)
    return final


def make_profile(service, *, name="Monthly Report", mode="operations", filenames=("Report.pdf",), ops=None,
                 master_bytes=None, timestamp_policy=None, output=None, options=None, reference=None,
                 enabled=True, **rules) -> Profile:
    from prm import operations as O
    prof = Profile(id=None, name=name, enabled=enabled, mode=mode)
    prof.rules.update({"filenames": list(filenames), **rules})
    if reference is not None:
        from prm.pdfinfo import fingerprint_file
        prof.reference_fingerprint = fingerprint_file(reference)
    if mode == "operations":
        prof.operations = ops if ops is not None else [
            O.new_op("replace_text", find="ACME Corporation", replace="Globex Inc"),
            O.new_op("add_text", page=0, rect=[300, 400, 540, 440], text="APPROVED"),
        ]
    else:
        tmpm = service.paths.root / "incoming-master.pdf"
        tmpm.write_bytes(master_bytes or samples.report_pdf(customer="Master Edited Co"))
        set_times(tmpm, created_ns=time.time_ns() - 400 * DAY, modified_ns=time.time_ns() - 400 * DAY)
        prof.master_file_id = service.repo.store_file(tmpm, "master")
        tmpm.unlink()
    if timestamp_policy:
        prof.timestamp_policy = timestamp_policy
    if output:
        prof.output.update(output)
    if options:
        prof.options.update(options)
    return service.repo.save_profile(prof, note="test")


def detect_and_process(service, path: Path) -> int:
    """Run the watcher's completion detection + worker synchronously for one file."""
    from prm.watcher import DownloadWatcher
    w = DownloadWatcher(service.repo, service.processor, [path.parent], stable_seconds=0, use_observer=False)
    w.candidate(path)
    w.stabilize_once()                    # first observation
    ids = w.stabilize_once(force_now=True)
    while w.run_next():
        pass
    return ids[0] if ids else None


def text_of(path) -> str:
    import pymupdf
    with pymupdf.open(str(path)) as d:
        return "".join(p.get_text() for p in d)


def times(path):
    return get_times(path)
