"""GUI tests (pytest-qt). Drive the real widgets programmatically in isolated data directories."""
import pymupdf
import pytest
from conftest import chrome_download, detect_and_process, make_profile, text_of

from prm import samples

pytest.importorskip("pytestqt")


@pytest.fixture
def win(qtbot, service, monkeypatch):
    import prm.ui.editor as ed
    import prm.ui.pages as pg
    import prm.ui.profile_dialog as pd
    for mod in (ed, pg, pd):
        monkeypatch.setattr(mod, "confirm", lambda *a, **k: True)
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    service.set_settings(minimize_to_tray=False)
    from prm.ui.main_window import MainWindow
    w = MainWindow(service)
    qtbot.addWidget(w)
    w.show()
    yield w
    w.timer.stop()
    w.tray.hide()


def test_all_pages_render(win, service, downloads):
    make_profile(service)
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf())
    detect_and_process(service, f)
    for key in ("dashboard", "profiles", "jobs", "settings", "diagnostics", "editor"):
        win.go(key)
    win.go("jobs")
    jobs = win.pages["jobs"]
    jobs.refresh()
    jobs.table.selectRow(0)
    html = jobs.details.toHtml()
    assert "Output timestamps" in html and "Verified" in html
    assert win.pages["profiles"].table.rowCount() == 1


def test_editor_interactions_end_to_end(win, service, tmp_path, downloads):
    import prm.operations as O
    ed = win.pages["editor"]
    win.go("editor")
    pdf = tmp_path / "Report.pdf"
    samples.report_pdf(pdf)
    assert ed.open_file(pdf)
    # add a text box by dragging on the canvas
    ed.on_create("text", pymupdf.Rect(72, 600, 300, 640), {"click": False})
    op = ed.session.ops[-1]
    assert op["type"] == "add_text" and ed.selected == op["id"]
    # edit its text through the properties panel signal
    ed.on_props_changed(op["id"], {"text": "Hello from UI", "size": 14.0})
    assert "Hello from UI" in ed.session.document()[0].get_text()
    # move + resize the object (as the canvas does on mouse release)
    ed.on_geometry(op["id"], pymupdf.Rect(100, 650, 400, 700))
    assert ed.session.find(op["id"])["rect"] == [100, 650, 400, 700]
    # draw shapes and markup
    ed.on_create("rect", pymupdf.Rect(50, 450, 150, 500), {"click": False})
    ed.on_create("arrow", pymupdf.Rect(200, 450, 300, 500), {"points": [[0, 0], [1, 1]]})
    ed.on_create("ink", pymupdf.Rect(320, 450, 400, 500), {"strokes": [[[0, 0], [1, 1]]]})
    ed.on_create("highlight", pymupdf.Rect(60, 125, 300, 135), {"click": False})
    assert {a.type[1] for a in ed.session.document()[0].annots()} >= {"Square", "Line", "Ink", "Highlight"}
    # undo / redo
    n = len(ed.session.ops)
    ed.undo()
    assert len(ed.session.ops) == n - 1
    ed.redo()
    assert len(ed.session.ops) == n
    # genuine edit of existing text via the dialog path
    ed.session.add(O.new_op("replace_text", find="ACME Corporation", replace="Initech", page=0,
                            area=ed.session.page_rect_to_media(0, pymupdf.Rect(60, 100, 300, 120))))
    ed.refresh_all()
    assert "Initech" in ed.session.document()[0].get_text()
    # page operations through menu handlers
    ed._rotate(90)
    ed._move_page(1)
    assert ed.page == 1 and ed.session.document()[1].rotation == 90
    # compare + diff view
    ed.a_compare.setChecked(True)
    ed.a_diff.setChecked(True)
    ed.refresh_page()
    assert ed.orig_box.isVisible()
    # search + replace all
    ed.search.setText("Lorem")
    ed.do_search()
    assert len(ed.hits) == 3
    ed.replace_with.setText("LOREM")
    ed.replace_all()
    assert "LOREM" in "".join(p.get_text() for p in ed.session.document())
    # save into an existing profile, then reopen it from the database
    prof = make_profile(service, enabled=False)
    ed.profile_id = prof.id
    assert ed.save_to_profile()
    saved = service.repo.get_profile(prof.id)
    assert len(saved.operations) == len(ed.session.ops) and saved.base_file_id
    ed.session.dirty = False
    ed.open_profile(prof.id)
    assert len(ed.session.ops) == len(saved.operations)
    # the saved operations now run on a real download
    service.repo.set_enabled(prof.id, True)
    f = chrome_download(downloads, "Report.pdf", samples.report_pdf())
    j = service.repo.job(detect_and_process(service, f))
    assert j["state"] == "done", j["error"]
    assert "Hello from UI" in text_of(f) and "Initech" in text_of(f)


def test_canvas_mapping_on_rotated_page(qtbot):
    from prm.ui.canvas import PageView
    d = pymupdf.open()
    p = d.new_page(width=200, height=400)
    p.set_rotation(90)
    v = PageView()
    qtbot.addWidget(v)
    v.show_page(p, 2.0)
    r = pymupdf.Rect(10, 20, 50, 60)
    back = v.scene_to_page(v.page_to_scene(r))
    assert [round(x, 3) for x in back] == [10, 20, 50, 60]
    q = v.page_to_scene(r)
    assert q.width() == 80 and q.height() == 80


def test_nl_assistant_flow(win, tmp_path):
    ed = win.pages["editor"]
    pdf = tmp_path / "Report.pdf"
    samples.report_pdf(pdf)
    ed.open_file(pdf)
    ed.nl_input.setPlainText('replace "ACME Corporation" with "Umbrella"; add page numbers')
    ed.nl_propose()
    assert ed.nl_list.count() == 2
    assert len(ed.session.ops) == 0           # nothing applied before confirmation
    ed.nl_accept()
    assert len(ed.session.ops) == 2


def test_profile_dialog_collects_and_confirms(win, service, qtbot):
    from prm.ui.profile_dialog import ProfileDialog
    prof = make_profile(service, enabled=False)
    dlg = ProfileDialog(service, prof)
    qtbot.addWidget(dlg)
    dlg.enabled.setChecked(True)
    dlg.ts_radios["custom"].setChecked(True)
    dlg.c_src.setCurrentIndex(dlg.c_src.findData("fixed"))
    dlg._save()
    p = service.repo.get_profile(prof.id)
    assert p.enabled and p.timestamp_policy["kind"] == "custom" and p.timestamp_policy["created_fixed"]
    assert p.version_no == 2
