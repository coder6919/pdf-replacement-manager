"""End-to-end self test, runnable from the packaged executable: `PDFReplacementManager.exe --self-test`.

Uses only temporary directories and generated sample PDFs.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import traceback
from pathlib import Path


def run() -> dict:
    from . import operations as O
    from . import samples
    from .backup import export_backup, restore_backup
    from .editor_model import EditSession
    from .repository import Profile
    from .service import AppService
    from .timestamps import get_times, set_times

    root = Path(tempfile.mkdtemp(prefix="prm-selftest-"))
    checks: dict[str, str] = {}

    def check(name, cond, detail=""):
        checks[name] = "ok" if cond else f"FAIL {detail}"

    svc = None
    try:
        dl = root / "Downloads"
        dl.mkdir()
        svc = AppService(root / "data", use_observer=True)
        svc.set_settings(watch_folders=[str(dl)], stable_seconds=0.5)
        svc.processor.opener = svc.processor.chrome_opener = lambda p: None
        prof = Profile(id=None, name="Self test", enabled=True)
        prof.rules["filenames"] = ["Report.pdf"]
        prof.operations = [O.new_op("replace_text", find="ACME Corporation", replace="Globex Inc"),
                           O.new_op("add_text", page=0, rect=[300, 400, 540, 440], text="APPROVED")]
        prof = svc.repo.save_profile(prof)
        svc.start()
        old = time.time_ns() - 10 * 86400 * 10**9

        def download(name, data):
            part = dl / f"{name}.crdownload"
            part.write_bytes(data)
            set_times(part, created_ns=old, modified_ns=old)
            os.replace(part, dl / name)
            return dl / name

        f = download("Report.pdf", samples.report_pdf())
        other = download("Unrelated.pdf", samples.report_pdf())
        other_bytes = other.read_bytes()
        deadline = time.time() + 60
        while time.time() < deadline and not any(j["state"] == "done" for j in svc.repo.jobs()):
            time.sleep(0.25)
        jobs = svc.repo.jobs()
        done = [j for j in jobs if j["state"] == "done"]
        check("watcher detected and processed download", bool(done), [(j["file_name"], j["state"], j["error"])
                                                                       for j in jobs])
        import pymupdf
        with pymupdf.open(f) as d:
            t = "".join(p.get_text() for p in d)
        check("saved edits applied", "Globex Inc" in t and "APPROVED" in t and "ACME" not in t)
        check("filesystem modified time = download time", get_times(f).modified_ns == (old // 100) * 100)
        check("unrelated PDF untouched", other.read_bytes() == other_bytes)
        check("original backed up", bool(done) and svc.repo.file_path(done[0]["backup_file_id"]).is_file())
        # editor round trip
        s = EditSession(samples.report_pdf(), session_dir=svc.paths.sessions)
        s.add(O.new_op("watermark", text="X"))
        s2 = EditSession.load_autosave(s.session_file())
        check("editor autosave reload", len(s2.ops) == 1)
        z = root / "b.zip"
        export_backup(svc, z)
        restore_backup(svc, z)
        check("backup + restore", svc.repo.get_profile(prof.id).name == "Self test")
    except Exception:
        checks["exception"] = traceback.format_exc()[-1500:]
    finally:
        if svc:
            try:
                svc.stop()
                svc.db.close()
            except Exception:
                pass
        shutil.rmtree(root, ignore_errors=True)
    return {"ok": all(v == "ok" for v in checks.values()), "checks": checks}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
