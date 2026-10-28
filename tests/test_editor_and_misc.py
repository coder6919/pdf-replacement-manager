import json
import time
import urllib.request
from pathlib import Path

import pymupdf
import pytest
from conftest import chrome_download, detect_and_process, text_of

from prm import operations as O
from prm import samples
from prm.editor_model import EditSession
from prm.nlassist import propose
from prm.repository import Profile


def test_undo_redo_and_incremental_render():
    s = EditSession(samples.report_pdf())
    a = O.new_op("add_text", page=0, rect=[72, 600, 300, 640], text="One")
    b = O.new_op("add_text", page=0, rect=[72, 650, 300, 690], text="Two")
    assert not s.add(a) and not s.add(b)
    assert "Two" in s.document()[0].get_text()
    s.undo()
    assert "Two" not in s.document()[0].get_text() and "One" in s.document()[0].get_text()
    s.redo()
    assert "Two" in s.document()[0].get_text()
    s.update(a["id"], rect=[300, 300, 500, 340])                    # move
    objs = s.objects_on_page(0)
    assert [round(r.x0) for op, r in objs if op["id"] == a["id"]] == [300]
    s.update(a["id"], rect=[300, 300, 560, 380])                    # resize
    s.remove(b["id"])
    assert "Two" not in s.document()[0].get_text()
    assert s.add(O.new_op("add_text", page=0, rect=[5, 5, 1, 1])), "invalid op must be rejected"


def test_object_mapping_follows_page_moves():
    s = EditSession(samples.report_pdf())
    s.add(O.new_op("add_text", page=2, rect=[72, 600, 300, 640], text="On third"))
    s.add(O.new_op("move_page", page=2, to=0))
    assert [op["text"] for op, _ in s.objects_on_page(0)] == ["On third"]
    s.add(O.new_op("delete_pages", pages="1"))
    assert s.objects_on_page(0) == [] and s.page_count == 2


def test_autosave_close_reopen_continue_then_master_and_pipeline(service, downloads, tmp_path):
    """Acceptance test for editing: import, edit, move/resize, page ops, save, close, reopen, continue,
    save as persistent master, and have download monitoring reuse it."""
    base = samples.report_pdf()
    s = EditSession(base, base_name="Report.pdf", session_dir=service.paths.sessions,
                    asset_resolver=service.repo.asset_resolver)
    t = O.new_op("add_text", page=0, rect=[72, 600, 300, 640], text="Edited")
    s.add(t)
    s.update(t["id"], rect=[100, 620, 360, 680])
    s.add(O.new_op("replace_text", find="ACME Corporation", replace="Initech LLC"))
    s.add(O.new_op("rotate_pages", pages="3", angle=90))
    sid = s.session_id
    del s                                                            # application closed without saving
    saves = EditSession.list_autosaves(service.paths.sessions)
    assert saves and saves[0]["ops"] == 3
    s2 = EditSession.load_autosave(saves[0]["path"], asset_resolver=service.repo.asset_resolver)
    assert s2.session_id == sid and len(s2.ops) == 3
    s2.add(O.new_op("watermark", text="FINAL"))                     # continue editing
    master = tmp_path / "master.pdf"
    s2.export(master)
    assert "Initech LLC" in text_of(master) and "FINAL" in text_of(master)
    # persistent master profile
    prof = Profile(id=None, name="From editor", enabled=True, mode="master")
    prof.rules["filenames"] = ["Report.pdf"]
    prof.master_file_id = service.repo.store_file(master, "master")
    prof.operations = s2.ops
    prof = service.repo.save_profile(prof)
    f = chrome_download(downloads, "Report.pdf", base)
    j = service.repo.job(detect_and_process(service, f))
    assert j["state"] == "done" and "Initech LLC" in text_of(f) and "Edited" in text_of(f)
    # switch the same profile to operations mode: saved operations are replayed on the next download
    prof.mode = "operations"
    service.repo.save_profile(prof)
    f.unlink()
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf(total="3.00"))
    j = service.repo.job(detect_and_process(service, f))
    assert j["state"] == "done", j["error"]
    t = text_of(f)
    assert "Initech LLC" in t and "3.00" in t and "FINAL" in t
    with pymupdf.open(f) as d:
        assert d[2].rotation == 90


def test_export_split_pages(tmp_path):
    s = EditSession(samples.report_pdf())
    s.export_pages(tmp_path / "p2.pdf", [1])
    with pymupdf.open(tmp_path / "p2.pdf") as d:
        assert d.page_count == 1 and "Page 2" in d[0].get_text()


def test_password_pdf_cannot_be_edited():
    with pytest.raises(ValueError):
        EditSession(samples.encrypted_pdf())


def test_nl_assistant_proposes_but_never_guesses():
    ops, notes = propose('replace "ACME" with "Globex"; add page numbers bottom right; delete page 3; '
                         'highlight "Total"; redact "secret"; do something vague', n_pages=5)
    types = [o["type"] for o in ops]
    assert types == ["replace_text", "page_numbers", "delete_pages", "markup", "redact"]
    assert ops[1]["position"] == "bottom-right" and ops[2]["pages"] == "3"
    assert any("not understood" in n for n in notes)
    ops, notes = propose("replace the total", 1)
    assert not ops and notes


def test_bridge_requires_token_and_rejects_web_origins(service):
    service.set_settings(bridge_enabled=True, bridge_port=47999)
    service.start_bridge()
    token = service.db.get_setting("bridge_token")
    try:
        def call(path, body=None, headers=None):
            req = urllib.request.Request(f"http://127.0.0.1:47999{path}", data=json.dumps(body).encode()
                                         if body else None, headers=headers or {}, method="POST" if body else "GET")
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, None
        assert call("/v1/ping")[0] == 403
        assert call("/v1/ping", headers={"X-PRM-Token": "wrong"})[0] == 403
        assert call("/v1/ping", headers={"X-PRM-Token": token, "Origin": "https://evil.example"})[0] == 403
        ok = {"X-PRM-Token": token, "Content-Type": "application/json"}
        assert call("/v1/ping", headers=ok)[0] == 200
        assert call("/v1/hint", {"file_name": "C:\\x\\Report.pdf", "url": "https://site.example/r.pdf"}, ok)[0] == 200
        assert service.bridge.hint_for("Report.pdf")["url"] == "https://site.example/r.pdf"
        assert call("/v1/status", headers=ok)[0] == 200
    finally:
        service.stop_bridge()


def test_verify_all_managed_files_detects_tampering(service, tmp_path):
    f = tmp_path / "m.pdf"
    samples.report_pdf(f)
    fid = service.repo.store_file(f, "master")
    assert all(r["ok"] for r in service.repo.verify_all_files())
    p = service.repo.file_path(fid)
    p.write_bytes(b"tampered")
    bad = [r for r in service.repo.verify_all_files() if not r["ok"]]
    assert bad and "integrity" in bad[0]["message"]


def test_original_retention_policy(service, downloads):
    from conftest import make_profile
    make_profile(service)
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf())
    jid = detect_and_process(service, f)
    fid = service.repo.job(jid)["backup_file_id"]
    assert service.repo.purge_originals(keep_days=30) == 0
    service.db.x("UPDATE managed_files SET created_at=? WHERE id=?", (time.time_ns() - 40 * 86400 * 10**9, fid))
    assert service.repo.purge_originals(keep_days=30) == 1
    assert service.repo.job(jid)["backup_file_id"] is None
