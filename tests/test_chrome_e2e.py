"""Real Google Chrome download, end to end (opt-in: set PRM_CHROME_E2E=1).

A local HTTP server serves a generated PDF as an attachment; headless Chrome with a throw-away profile
downloads it into a temporary folder; the application's watcher (real ReadDirectoryChangesW events) detects the
completed download (.crdownload -> .pdf), processes it, and the test verifies content, source host from
Chrome's Zone.Identifier, timestamps, and Chrome's ' (1)' collision naming on a second download.
"""
import http.server
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pymupdf
import pytest

from prm import samples
from prm.fsutil import find_chrome
from prm.service import AppService
from prm.timestamps import get_times

pytestmark = pytest.mark.skipif(os.environ.get("PRM_CHROME_E2E") != "1" or not find_chrome(),
                                reason="set PRM_CHROME_E2E=1 (requires Google Chrome)")


def serve(pdf_versions):
    state = {"n": 0}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            data = pdf_versions[min(state["n"], len(pdf_versions) - 1)]
            state["n"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", 'attachment; filename="Report.pdf"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def chrome_download(profile_dir: Path, dl: Path, url: str):
    (profile_dir / "Default").mkdir(parents=True, exist_ok=True)
    prefs = {"download": {"default_directory": str(dl), "prompt_for_download": False, "directory_upgrade": True},
             "plugins": {"always_open_pdf_externally": True}, "safebrowsing": {"enabled": False}}
    (profile_dir / "Default" / "Preferences").write_text(json.dumps(prefs))
    return subprocess.Popen([find_chrome(), "--headless=new", f"--user-data-dir={profile_dir}", "--no-first-run",
                             "--disable-sync", "--no-default-browser-check", url],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for(cond, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.25)
    return False


def test_real_chrome_download_is_processed(tmp_path):
    from prm import operations as O
    from prm.repository import Profile
    dl = tmp_path / "Downloads"
    dl.mkdir()
    srv = serve([samples.report_pdf(), samples.report_pdf(total="42.00")])
    url = f"http://127.0.0.1:{srv.server_address[1]}/files/Report.pdf?token=SECRET123"
    svc = AppService(tmp_path / "data", use_observer=True)
    svc.set_settings(watch_folders=[str(dl)], stable_seconds=1.0)
    svc.processor.opener = svc.processor.chrome_opener = lambda p: None
    prof = Profile(id=None, name="Chrome report", enabled=True)
    prof.rules.update({"filenames": ["Report.pdf"], "hosts": ["127.0.0.1"]})
    prof.operations = [O.new_op("replace_text", find="ACME Corporation", replace="Globex Inc"),
                       O.new_op("watermark", text="PROCESSED")]
    svc.repo.save_profile(prof)
    svc.start()
    procs = []
    try:
        procs.append(chrome_download(tmp_path / "chrome1", dl, url))
        assert wait_for(lambda: any(j["state"] == "done" for j in svc.repo.jobs())), \
            [(j["file_name"], j["state"], j["error"]) for j in svc.repo.jobs()] or list(dl.iterdir())
        j = next(j for j in svc.repo.jobs() if j["state"] == "done")
        out = dl / "Report.pdf"
        with pymupdf.open(out) as d:
            t = "".join(p.get_text() for p in d)
        assert "Globex Inc" in t and "ACME" not in t and "PROCESSED" in t
        assert j["source_url"].startswith("http://127.0.0.1/files/Report.pdf")   # from Chrome's Zone.Identifier
        assert "SECRET123" not in (j["source_url"] or "")                       # query string not stored
        assert get_times(out).modified_ns == (j["download_modified_ns"] // 100) * 100
        assert not list(dl.glob("*.crdownload"))
        # second download of the same file: Chrome names it 'Report (1).pdf'
        procs.append(chrome_download(tmp_path / "chrome2", dl, url))
        assert wait_for(lambda: sum(j["state"] == "done" for j in svc.repo.jobs()) >= 2), \
            [(j["file_name"], j["state"], j["error"]) for j in svc.repo.jobs()]
        second = dl / "Report (1).pdf"
        assert second.exists()
        with pymupdf.open(second) as d:
            t2 = "".join(p.get_text() for p in d)
        assert "Globex Inc" in t2 and "42.00" in t2
        # our own outputs were not re-processed (no loops)
        assert all(j["state"] in ("done",) for j in svc.repo.jobs()), [(j["file_name"], j["state"]) for j in
                                                                        svc.repo.jobs()]
    finally:
        for p in procs:
            p.kill()
        svc.stop()
        srv.shutdown()
        svc.db.close()
        time.sleep(0.5)
        for d in ("chrome1", "chrome2"):
            shutil.rmtree(tmp_path / d, ignore_errors=True)
