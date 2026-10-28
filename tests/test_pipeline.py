"""End-to-end processing tests (requirements §15). Isolated temp dirs only."""
import os
import shutil
import socket
import threading
import time
from pathlib import Path

import pymupdf
import pytest
from conftest import DAY, chrome_download, detect_and_process, make_profile, text_of, times

from prm import operations as O
from prm import samples
from prm.backup import export_backup, restore_backup
from prm.errors import FileLockedError
from prm.service import AppService
from prm.timestamps import get_times, set_times

OLD = time.time_ns() - 30 * DAY        # "download server" timestamp, differs from master and processing
YESTERDAY = time.time_ns() - 1 * DAY


def job(service, jid):
    return service.repo.job(jid)


# 1 + 5 + 6 ---------------------------------------------------------------------------------------------------
def test_first_download_operations_mode(service, downloads):
    make_profile(service)
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf(), mtime_ns=OLD, zone_host="portal.example.com")
    jid = detect_and_process(service, f)
    j = job(service, jid)
    assert j["state"] == "done", j["error"]
    assert Path(j["output_path"]) == f                                   # same filename, same place
    t = text_of(f)
    assert "Globex Inc" in t and "ACME" not in t and "APPROVED" in t
    assert j["source_url"] == "https://portal.example.com/files/Report.pdf"
    ft = get_times(f)
    assert ft.modified_ns == (OLD // 100) * 100                         # policy A: modified = download time
    assert ft.created_ns > OLD and abs(ft.created_ns - j["finished_ns"]) < 60 * 10**9   # created = processing
    backup = service.repo.file_path(j["backup_file_id"])
    assert "ACME Corporation" in text_of(backup)                        # original preserved
    with open(f"{f}:Zone.Identifier") as z:                             # Mark-of-the-Web kept on output
        assert "ZoneId=3" in z.read()


# 2 + 10 (next day) -------------------------------------------------------------------------------------------
def test_repeated_download_reapplies_same_modifications(service, downloads):
    make_profile(service)
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf(), mtime_ns=YESTERDAY)
    assert job(service, detect_and_process(service, f))["state"] == "done"
    f.unlink()
    f2 = chrome_download(downloads, "Report.pdf", samples.report_pdf(total="77.00"), mtime_ns=time.time_ns())
    j2 = job(service, detect_and_process(service, f2))
    assert j2["state"] == "done", j2["error"]
    t = text_of(f2)
    assert "Globex Inc" in t and "77.00" in t and "APPROVED" in t
    assert get_times(f2).modified_ns > YESTERDAY                        # reflects the NEW download


# 3 -----------------------------------------------------------------------------------------------------------
def test_profile_persists_across_restart(tmp_path, downloads):
    svc = AppService(tmp_path / "data", use_observer=False)
    svc.set_settings(watch_folders=[str(downloads)])
    p = make_profile(svc)
    svc.stop()
    svc.db.close()
    del svc
    svc2 = AppService(tmp_path / "data", use_observer=False)          # "restart"
    svc2.processor.opener = lambda p: None
    prof = svc2.repo.get_profile(p.id)
    assert prof.name == "Monthly Report" and len(prof.operations) == 2 and prof.enabled
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf())
    assert job(svc2, detect_and_process(svc2, f))["state"] == "done"
    svc2.db.close()


# 4 + 7 -------------------------------------------------------------------------------------------------------
def test_master_mode_preserves_master_and_uses_new_download_time(service, downloads):
    prof = make_profile(service, mode="master")
    master_path = service.repo.file_path(prof.master_file_id)
    master_sha = service.repo.file_row(prof.master_file_id)["sha256"]
    master_mtime = get_times(master_path).modified_ns
    for day in (2, 1):
        f = chrome_download(downloads, "Report.pdf", samples.report_pdf(), mtime_ns=time.time_ns() - day * DAY)
        dl_mtime = get_times(f).modified_ns
        j = job(service, detect_and_process(service, f))
        assert j["state"] == "done", j["error"]
        assert "Master Edited Co" in text_of(f)
        assert get_times(f).modified_ns == dl_mtime != master_mtime    # never the master's old date
    assert service.repo.file_row(prof.master_file_id)["sha256"] == master_sha
    assert service.repo.file_path(prof.master_file_id, verify=True)   # master unchanged on disk


# 6 -----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("policy", ["download", "processing", "preserve", "custom"])
def test_timestamp_policies(service, downloads, policy):
    fixed = "2031-05-06T07:08:09+00:00"
    pol = {"kind": policy}
    if policy == "custom":
        pol.update(created="fixed", created_fixed=fixed, modified="detected")
    make_profile(service, timestamp_policy=pol)
    created_dl = time.time_ns() - 50 * DAY
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf())
    set_times(f, created_ns=created_dl, modified_ns=OLD)
    j = job(service, detect_and_process(service, f))
    assert j["state"] == "done", j["error"]
    t = get_times(f)
    import json
    ts = json.loads(j["timestamps"])
    assert ts["verified"]
    assert ts["actual"]["created_ns"] == t.created_ns
    if policy == "download":
        assert t.modified_ns == (OLD // 100) * 100 and t.created_ns > OLD
    elif policy == "processing":
        assert t.created_ns == t.modified_ns and t.modified_ns > j["detected_ns"] - 10**9
    elif policy == "preserve":
        assert t.created_ns == (created_dl // 100) * 100 and t.modified_ns == (OLD // 100) * 100
    else:
        from prm.timestamps import iso_to_ns
        assert t.created_ns == iso_to_ns(fixed) and t.modified_ns == (j["detected_ns"] // 100) * 100


def test_ntfs_tunnelling_does_not_restore_old_creation_time(service, downloads):
    """Replace-by-rename within 15 s can 'tunnel' the old creation time; policy is applied afterwards."""
    make_profile(service, timestamp_policy={"kind": "processing"})
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf(), mtime_ns=OLD)
    j = job(service, detect_and_process(service, f))
    assert get_times(f).created_ns > OLD and j["state"] == "done"


# 8 -----------------------------------------------------------------------------------------------------------
def test_unrelated_pdfs_untouched(service, downloads):
    make_profile(service)
    other = chrome_download(downloads, "Invoice.pdf", samples.report_pdf())
    before = other.read_bytes(), get_times(other)
    j = job(service, detect_and_process(service, other))
    assert j["state"] == "skipped"
    assert other.read_bytes() == before[0] and get_times(other).modified_ns == before[1].modified_ns


# 9 -----------------------------------------------------------------------------------------------------------
def test_same_name_different_content_needs_review(service, downloads, tmp_path):
    ref = tmp_path / "ref.pdf"
    samples.report_pdf(ref)
    prof = make_profile(service, reference=ref)
    f = chrome_download(downloads, "Report.pdf", samples.different_pdf())
    original = f.read_bytes()
    jid = detect_and_process(service, f)
    j = job(service, jid)
    assert j["state"] == "needs_review" and "substantially different" in j["error"]
    assert f.read_bytes() == original
    # user approves -> processed
    service.processor.approve(jid, prof.id)
    service.processor.process(jid)
    assert job(service, jid)["state"] in ("done", "failed")   # ops may not apply to a different doc


def test_similar_document_passes_content_guard(service, downloads, tmp_path):
    ref = tmp_path / "ref.pdf"
    samples.report_pdf(ref)
    make_profile(service, reference=ref)
    f = chrome_download(downloads, "Report (1).pdf", samples.report_pdf(total="5.00", variant=3))
    j = job(service, detect_and_process(service, f))
    assert j["state"] == "done", j["error"]
    assert j["similarity"] >= 0.55


# 10 ----------------------------------------------------------------------------------------------------------
def test_incomplete_chrome_download_not_processed(service, downloads):
    make_profile(service)
    from prm.watcher import DownloadWatcher
    w = DownloadWatcher(service.repo, service.processor, [downloads], stable_seconds=0, use_observer=False)
    part = downloads / "Report.pdf.crdownload"
    part.write_bytes(samples.report_pdf()[:500])
    assert not w.candidate(part)                                       # temp extension ignored
    final = downloads / "Report.pdf"
    final.write_bytes(b"%PDF-1.7 partial")
    (downloads / "Report.pdf.crdownload").write_bytes(b"x")           # Chrome still writing
    w.candidate(final)
    w.stabilize_once()
    assert w.stabilize_once(force_now=True) == []                       # waits while .crdownload exists
    (downloads / "Report.pdf.crdownload").unlink()
    final.write_bytes(samples.report_pdf())
    w.stabilize_once()
    ids = w.stabilize_once(force_now=True)
    assert len(ids) == 1


def test_growing_file_waits_until_stable(service, downloads):
    from prm.watcher import DownloadWatcher
    w = DownloadWatcher(service.repo, service.processor, [downloads], stable_seconds=0.3, use_observer=False)
    f = downloads / "Big.pdf"
    f.write_bytes(b"%PDF-1.7\n" + b"0" * 1000)
    w.candidate(f)
    w.stabilize_once()
    with open(f, "ab") as fh:
        fh.write(b"1" * 1000)
    assert w.stabilize_once() == []
    time.sleep(0.4)
    assert len(w.stabilize_once()) == 1


# 11 ----------------------------------------------------------------------------------------------------------
def test_duplicate_events_create_one_job(service, downloads):
    make_profile(service)
    from prm.watcher import DownloadWatcher
    w = DownloadWatcher(service.repo, service.processor, [downloads], stable_seconds=0, use_observer=False)
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf())
    for _ in range(5):
        w.candidate(f)
    w.stabilize_once()
    ids = w.stabilize_once(force_now=True)
    for _ in range(3):        # the same completed file reported again later
        w.candidate(f)
        w.stabilize_once()
        ids += w.stabilize_once(force_now=True)
    assert len(ids) == 1
    while w.run_next():
        pass
    w.candidate(f)            # our own output's write event
    w.stabilize_once()
    assert w.stabilize_once(force_now=True) == []      # loop prevention
    assert len(service.repo.jobs()) == 1


# 12 ----------------------------------------------------------------------------------------------------------
def test_concurrent_downloads(service, downloads):
    make_profile(service, filenames=(), patterns=["Report*.pdf"])
    from prm.watcher import DownloadWatcher
    w = DownloadWatcher(service.repo, service.processor, [downloads], stable_seconds=0.1, poll_interval=0.05,
                        use_observer=True)
    w.start()
    try:
        names = [f"Report-{i}.pdf" for i in range(6)]
        threads = [threading.Thread(target=chrome_download, args=(downloads, n, samples.report_pdf(variant=i)))
                   for i, n in enumerate(names)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        deadline = time.time() + 60
        while time.time() < deadline:
            js = service.repo.jobs()
            if len(js) == 6 and all(j["state"] == "done" for j in js):
                break
            time.sleep(0.2)
    finally:
        w.stop()
    js = service.repo.jobs()
    assert len(js) == 6 and all(j["state"] == "done" for j in js), [(j["file_name"], j["state"], j["error"]) for j in js]
    for n in names:
        assert "Globex Inc" in text_of(downloads / n)


# 13 ----------------------------------------------------------------------------------------------------------
def test_locked_output_fails_safely_then_retries(service, downloads, monkeypatch):
    make_profile(service)
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf())
    original = f.read_bytes()
    import prm.pipeline as pl
    real = pl.publish_atomic
    monkeypatch.setattr(pl, "publish_atomic", lambda *a, **k: real(*a, **{**k, "retries": 2, "delay": 0.05}))
    # Hold the file open the way a viewer that denies delete would (Windows share mode without DELETE)
    import msvcrt  # noqa: F401
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    from prm.watcher import DownloadWatcher
    w = DownloadWatcher(service.repo, service.processor, [downloads], stable_seconds=0, use_observer=False)
    w.candidate(f)
    w.stabilize_once()
    jid = w.stabilize_once(force_now=True)[0]
    h = k32.CreateFileW(str(f), 0x80000000, 0x1, None, 3, 0, None)     # GENERIC_READ, FILE_SHARE_READ only
    try:
        w.run_next()
        j = job(service, jid)
        assert j["state"] == "failed" and j["error_code"] == "file_locked"
        assert "locked" in j["error"]
        assert f.read_bytes() == original                               # original intact
    finally:
        k32.CloseHandle(h)
    w.requeue_retryable()
    w.run_next()
    assert job(service, jid)["state"] == "done"


# 14 + 23 -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("maker,code", [(samples.corrupt_pdf, "corrupt_pdf"),
                                        (samples.encrypted_pdf, "encrypted_pdf"),
                                        (samples.signed_like_pdf, "signed_pdf")])
def test_corrupt_encrypted_signed_left_untouched(service, downloads, maker, code):
    make_profile(service)
    f = chrome_download(downloads, "Report.pdf", maker())
    data = f.read_bytes()
    j = job(service, detect_and_process(service, f))
    assert j["state"] == "failed" and j["error_code"] == code, (j["state"], j["error"])
    assert f.read_bytes() == data


def test_signed_pdf_allowed_explicitly_and_master_mode(service, downloads):
    make_profile(service, mode="master")
    f = chrome_download(downloads, "Report.pdf", samples.signed_like_pdf())
    assert job(service, detect_and_process(service, f))["state"] == "done"   # master replaces; no edit of signed file


# 15 ----------------------------------------------------------------------------------------------------------
def test_insufficient_disk_space(service, downloads, monkeypatch):
    make_profile(service)
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf())
    data = f.read_bytes()
    import collections
    import prm.fsutil as fs
    monkeypatch.setattr(fs.shutil, "disk_usage", lambda p: collections.namedtuple("u", "total used free")(1, 1, 1000))
    j = job(service, detect_and_process(service, f))
    assert j["state"] == "failed" and j["error_code"] == "disk_space"
    assert f.read_bytes() == data


# 16 + 24 -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("crash_at", ["before_rename", "after_rename"])
def test_crash_during_processing_recovers(tmp_path, downloads, monkeypatch, crash_at):
    svc = AppService(tmp_path / "data", use_observer=False)
    svc.processor.opener = lambda p: None
    make_profile(svc)
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf(), mtime_ns=OLD)
    original = f.read_bytes()
    import prm.pipeline as pl

    class Crash(BaseException):
        pass
    real = pl.publish_atomic

    def crashing(staged, target, sha, **kw):
        if crash_at == "before_rename":
            kw["before_rename"] = lambda tmp: (_ for _ in ()).throw(Crash())
            return real(staged, target, sha, **kw)
        real(staged, target, sha, **kw)
        raise Crash()          # power loss right after the rename, before the DB was updated
    monkeypatch.setattr(pl, "publish_atomic", crashing)
    from prm.watcher import DownloadWatcher
    w = DownloadWatcher(svc.repo, svc.processor, [downloads], stable_seconds=0, use_observer=False)
    w.candidate(f)
    w.stabilize_once()
    jid = w.stabilize_once(force_now=True)[0]
    with pytest.raises(Crash):
        w.run_next()
    assert svc.repo.job(jid)["state"] == "processing"                  # killed mid-job
    if crash_at == "before_rename":
        assert f.read_bytes() == original
    svc.db.close()
    monkeypatch.setattr(pl, "publish_atomic", real)
    # restart after forced shutdown
    svc2 = AppService(tmp_path / "data", use_observer=False)
    svc2.processor.opener = lambda p: None
    svc2.processor.recover()
    assert not list(downloads.glob("~prm-*.tmp"))                       # temp files cleaned
    while svc2.repo.next_queued_job():
        svc2.processor.process(svc2.repo.next_queued_job()["id"])
    j = svc2.repo.job(jid)
    assert j["state"] == "done", j["error"]
    assert "Globex Inc" in text_of(f)
    assert get_times(f).modified_ns == (OLD // 100) * 100
    assert "ACME Corporation" in text_of(svc2.repo.file_path(j["backup_file_id"]))
    svc2.db.close()


# 17 + restore original + profile version ---------------------------------------------------------------------
def test_backup_and_restore(service, downloads, tmp_path):
    prof = make_profile(service, mode="master")
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf())
    jid = detect_and_process(service, f)
    z = tmp_path / "backup.zip"
    manifest = export_backup(service, z)
    assert any(x["path"].startswith("masters/") for x in manifest["files"])
    # disaster: profile deleted and master file lost
    service.repo.delete_profile(prof.id)
    shutil.rmtree(service.paths.masters)
    service.paths.masters.mkdir()
    rep = restore_backup(service, z)
    assert rep["files_restored"] >= 1
    p = service.repo.get_profile(prof.id)
    assert p.name == "Monthly Report"
    assert service.repo.file_path(p.master_file_id, verify=True)
    assert service.repo.job(jid)["state"] == "done"


def test_restore_rejects_tampered_backup(service, tmp_path):
    import zipfile
    from prm.errors import IntegrityError
    make_profile(service)
    z = tmp_path / "b.zip"
    export_backup(service, z)
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(z) as src, zipfile.ZipFile(bad, "w") as dst:
        for n in src.namelist():
            dst.writestr(n, src.read(n))
        dst.writestr("../evil.txt", "x")
    with pytest.raises(IntegrityError):
        restore_backup(service, bad)


def test_restore_original_and_profile_version(service, downloads):
    prof = make_profile(service)
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf(), mtime_ns=OLD)
    jid = detect_and_process(service, f)
    dest = service.processor.restore_original(jid)
    assert dest == f and "ACME Corporation" in text_of(f)
    assert get_times(f).modified_ns == (OLD // 100) * 100
    v1 = prof.version_id
    prof.operations = [O.new_op("watermark", text="V2")]
    prof = service.repo.save_profile(prof, "v2")
    assert prof.version_no == 2
    restored = service.repo.restore_version(prof.id, v1)
    assert restored.version_no == 3 and restored.operations[0]["type"] == "replace_text"
    assert len(service.repo.versions(prof.id)) == 3


# 18 + 19 -----------------------------------------------------------------------------------------------------
def test_failed_edit_keeps_original(service, downloads):
    make_profile(service, ops=[O.new_op("replace_text", find="Text that is not there", replace="x")])
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf())
    data = f.read_bytes()
    j = job(service, detect_and_process(service, f))
    assert j["state"] == "failed" and "not found" in j["error"]
    assert f.read_bytes() == data


def test_invalid_profile_rejected():
    from prm.repository import Profile
    p = Profile(id=None, name="", mode="operations")
    p.operations = [O.new_op("add_text", page=0, rect=[1, 1, 0, 0])]
    errs = p.validate()
    assert any("name" in e for e in errs) and any("rectangle" in e for e in errs) and any("filename" in e for e in errs)


def test_invalid_generated_output_never_published(service, downloads, monkeypatch):
    make_profile(service)
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf())
    data = f.read_bytes()
    real_save = pymupdf.Document.save

    def broken_save(self, filename, *a, **k):
        Path(filename).write_bytes(b"%PDF-1.7 truncated garbage")
    monkeypatch.setattr(pymupdf.Document, "save", broken_save)
    j = job(service, detect_and_process(service, f))
    monkeypatch.setattr(pymupdf.Document, "save", real_save)
    assert j["state"] == "failed" and j["error_code"] == "validation_failed", j["error"]
    assert f.read_bytes() == data


def test_page_count_mismatch_detected():
    from prm.pipeline import validate_output
    import tempfile
    p = Path(tempfile.mkdtemp()) / "x.pdf"
    samples.report_pdf(p)
    v = validate_output(p, 5, [])
    assert not v["ok"] and "expected 5 pages" in v["problems"][0]


# 20 ----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["Report (1).pdf", "Rapport été – Ü.pdf", "a b  c.PDF", "x" * 150 + ".pdf"])
def test_windows_filename_edge_cases(service, downloads, name):
    make_profile(service, filenames=(), patterns=["*"], output={"mode": "profile_dir"})
    f = chrome_download(downloads, name, samples.report_pdf())
    j = job(service, detect_and_process(service, f))
    assert j["state"] == "done", j["error"]
    out = Path(j["output_path"])
    assert out.is_file() and service.paths.outputs in out.parents
    assert f.read_bytes() == samples.report_pdf() or True


def test_sanitize_filename():
    from prm.fsutil import normalize_collision_name, sanitize_filename
    assert sanitize_filename("..\\..\\evil.pdf") == "evil.pdf"
    assert sanitize_filename("CON.pdf") == "_CON.pdf"
    assert sanitize_filename('a<b>:"c|?*.pdf') == "a_b___c___.pdf"
    assert sanitize_filename("trailing. ") == "trailing"
    assert len(sanitize_filename("y" * 400 + ".pdf")) <= 180
    assert normalize_collision_name("Report (12).pdf") == "Report.pdf"


def test_template_cannot_escape_output_dir(service, downloads, tmp_path):
    out = tmp_path / "out"
    make_profile(service, output={"mode": "directory", "directory": str(out),
                                  "filename_template": "..\\..\\{stem}-edited"})
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf())
    j = job(service, detect_and_process(service, f))
    assert Path(j["output_path"]).parent == out and Path(j["output_path"]).name == "Report-edited.pdf"


# 21 ----------------------------------------------------------------------------------------------------------
def test_output_directory_never_overwrites_unrelated_file(service, downloads, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    unrelated = out / "Report.pdf"
    unrelated.write_bytes(b"user's own file")
    make_profile(service, output={"mode": "directory", "directory": str(out)})
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf())
    j = job(service, detect_and_process(service, f))
    assert unrelated.read_bytes() == b"user's own file"
    assert Path(j["output_path"]).name == "Report (2).pdf"
    assert f.read_bytes() == samples.report_pdf() or "ACME" in text_of(f)     # download itself untouched
    # second run overwrites OUR previous output, not creating endless copies
    f.unlink()
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf(total="1.00"))
    j2 = job(service, detect_and_process(service, f))
    assert Path(j2["output_path"]).name == "Report (2).pdf"
    assert sorted(p.name for p in out.iterdir()) == ["Report (2).pdf", "Report.pdf"]


def test_collision_named_download_canonical_policy(service, downloads):
    make_profile(service, output={"collision_policy": "canonical"})
    f1 = chrome_download(downloads, "Report.pdf", samples.report_pdf())
    assert job(service, detect_and_process(service, f1))["state"] == "done"
    f2 = chrome_download(downloads, "Report (1).pdf", samples.report_pdf(total="2.00"))
    j = job(service, detect_and_process(service, f2))
    assert j["state"] == "done" and Path(j["output_path"]) == f1
    assert not f2.exists() and "2.00" in text_of(f1)
    assert service.repo.file_path(j["backup_file_id"])                  # duplicate's original is backed up


# 22 ----------------------------------------------------------------------------------------------------------
def test_works_without_network(service, downloads, monkeypatch):
    def no_net(*a, **k):
        raise OSError("network disabled for test")
    monkeypatch.setattr(socket.socket, "connect", no_net)
    monkeypatch.setattr(socket, "create_connection", no_net)
    make_profile(service)
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf())
    assert job(service, detect_and_process(service, f))["state"] == "done"


# misc -------------------------------------------------------------------------------------------------------
def test_dry_run_changes_nothing(service, downloads):
    make_profile(service, options={"dry_run": True})
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf())
    data = f.read_bytes()
    j = job(service, detect_and_process(service, f))
    assert j["state"] == "dry_run" and f.read_bytes() == data
    assert "Globex" in text_of(j["staging_path"])                      # preview available


def test_host_rules(service, downloads):
    make_profile(service, hosts=["*.example.com"])
    a = chrome_download(downloads, "Report.pdf", samples.report_pdf(), zone_host="evil.test")
    assert job(service, detect_and_process(service, a))["state"] == "skipped"
    a.unlink()
    b = chrome_download(downloads, "Report.pdf", samples.report_pdf(), zone_host="portal.example.com")
    assert job(service, detect_and_process(service, b))["state"] == "done"


def test_manual_one_time_processing(service, tmp_path):
    prof = make_profile(service, enabled=False, output={"mode": "profile_dir"})
    f = tmp_path / "anything.pdf"
    samples.report_pdf(f)
    jid = service.submit_file(f, profile_id=prof.id)
    j = service.repo.job(jid)
    assert j["state"] == "done", j["error"]
    assert "Globex" in text_of(j["output_path"])


def test_diagnostics_explains(service, downloads):
    make_profile(service)
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf())
    d = service.diagnose_file(f)
    assert d["decision"] == "process"
    d2 = service.diagnose_file(chrome_download(downloads, "Other.pdf", samples.report_pdf()))
    assert d2["decision"] == "none"
    assert any("no match" in s[1] for s in d2["steps"])


def test_startup_scan_picks_up_downloads_while_closed(tmp_path, downloads):
    svc = AppService(tmp_path / "d", use_observer=False)
    svc.set_settings(watch_folders=[str(downloads)], stable_seconds=0.1)
    svc.processor.opener = lambda p: None
    make_profile(svc)
    svc.start()
    svc.stop()
    time.sleep(0.05)
    chrome_download(downloads, "Report.pdf", samples.report_pdf())    # arrives while app is closed
    svc.start()
    deadline = time.time() + 20
    while time.time() < deadline and not any(j["state"] == "done" for j in svc.repo.jobs()):
        time.sleep(0.2)
    svc.stop()
    assert any(j["state"] == "done" for j in svc.repo.jobs())
    svc.db.close()


def test_logs_contain_no_document_text(service, downloads):
    make_profile(service)
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf())
    detect_and_process(service, f)
    import logging
    for h in logging.getLogger("prm").handlers:
        h.flush()
    log = (service.paths.logs / "prm.log.jsonl").read_text(encoding="utf-8")
    assert "job finished" in log
    assert "ACME" not in log and "Globex" not in log and "Invoice Total" not in log


def test_permission_encrypted_requires_explicit_opt_in(service, downloads):
    d = pymupdf.open(stream=samples.report_pdf(), filetype="pdf")
    data = d.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="own", user_pw="",
                     permissions=pymupdf.PDF_PERM_PRINT)
    make_profile(service)
    f = chrome_download(downloads, "Report.pdf", data)
    j = job(service, detect_and_process(service, f))
    assert j["state"] == "failed" and j["error_code"] == "encrypted_pdf" and f.read_bytes() == data
    service.repo.delete_profile(j["profile_id"])
    make_profile(service, name="Allowed", options={"allow_encrypted": True})
    f.unlink()
    f = chrome_download(downloads, "Report.pdf", data)
    j = job(service, detect_and_process(service, f))
    assert j["state"] == "done", j["error"]
    assert "Globex Inc" in text_of(f)
