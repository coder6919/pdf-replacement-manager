"""Interactive PDF editor. Every edit is a replayable operation; see prm.operations."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pymupdf
from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QImage, QKeySequence, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton, QRadioButton, QSplitter, QTableWidget,
                               QTableWidgetItem, QTabWidget, QToolBar, QToolButton, QVBoxLayout, QWidget)

from .. import operations as O
from ..editor_model import EditSession
from ..errors import PRMError
from ..nlassist import propose
from ..pdfinfo import inspect_doc
from ..pdftext import find_text, selected_line_text, word_at, words_in_rect
from .canvas import PageView, render_page
from .common import EFFECT_TEXT, confirm, error, info_box
from .profile_dialog import ProfileDialog
from .props import PropertiesPanel

TOOLS = [  # (key, label, tooltip)
    ("select", "Select", "Select, move and resize added objects (Del removes)"),
    ("edit_text", "Edit text", "Click a word or drag across text on one line to replace or delete it (genuine edit)"),
    ("text", "Text", "Drag a box to add new text"),
    ("highlight", "Highlight", "Drag across text to highlight"),
    ("underline", "Underline", "Drag across text to underline"),
    ("strikeout", "Strike", "Drag across text to strike through"),
    ("note", "Note", "Click to add a sticky note"),
    ("comment", "Comment", "Drag to add a comment box"),
    ("rect", "Rectangle", "Drag to draw a rectangle"),
    ("ellipse", "Ellipse", "Drag to draw a circle/ellipse"),
    ("line", "Line", "Drag to draw a line"),
    ("arrow", "Arrow", "Drag to draw an arrow"),
    ("ink", "Freehand", "Draw freehand"),
    ("image", "Image", "Drag a box, then choose an image"),
    ("redact", "Redact", "Drag over content to remove it permanently (secure redaction)"),
    ("field", "Form field", "Drag to add a form field"),
    ("crop", "Crop", "Drag the area of this page to keep visible"),
]
ZOOMS = [0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0]


class EditorWidget(QWidget):
    profileSaved = Signal(int)
    statusMessage = Signal(str)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service, self.repo = service, service.repo
        self.session: EditSession | None = None
        self.profile_id: int | None = None
        self.page = 0
        self.zoom = 1.0
        self.selected: str | None = None
        self.hits: list = []
        self.hit_idx = -1
        self._thumb_rev = -1
        self._redact_explained = False
        self._build()
        self._update_enabled()

    # ================================================================== layout
    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        tb = QToolBar()
        tb.setIconSize(QSize(16, 16))
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.tb = tb
        self.a_open = tb.addAction("Open PDF…", self.open_pdf_dialog)
        self.a_open.setShortcut(QKeySequence.Open)
        prof_btn = QToolButton()
        prof_btn.setText("Open profile")
        prof_btn.setPopupMode(QToolButton.InstantPopup)
        self.profile_menu = QMenu(prof_btn)
        self.profile_menu.aboutToShow.connect(self._fill_profile_menu)
        prof_btn.setMenu(self.profile_menu)
        tb.addWidget(prof_btn)
        tb.addSeparator()
        self.a_save = tb.addAction("Save to profile", self.save_to_profile)
        self.a_save.setShortcut(QKeySequence.Save)
        self.a_save_as = tb.addAction("Save as new profile…", lambda: self.save_to_profile(new=True))
        self.a_master = tb.addAction("Save as master…", self.save_as_master)
        self.a_export = tb.addAction("Export PDF…", self.export_pdf)
        tb.addSeparator()
        self.a_undo = tb.addAction("↶ Undo", self.undo)
        self.a_undo.setShortcut(QKeySequence.Undo)
        self.a_redo = tb.addAction("↷ Redo", self.redo)
        self.a_redo.setShortcuts([QKeySequence.Redo, QKeySequence("Ctrl+Y")])
        tb.addSeparator()
        tb.addAction("−", lambda: self.step_zoom(-1)).setShortcut(QKeySequence.ZoomOut)
        self.zoom_box = QComboBox()
        for z in ZOOMS:
            self.zoom_box.addItem(f"{int(z * 100)}%", z)
        self.zoom_box.setCurrentIndex(ZOOMS.index(1.0))
        self.zoom_box.currentIndexChanged.connect(lambda i: self.set_zoom(self.zoom_box.itemData(i)))
        tb.addWidget(self.zoom_box)
        tb.addAction("+", lambda: self.step_zoom(1)).setShortcut(QKeySequence.ZoomIn)
        tb.addAction("Fit width", self.fit_width)
        tb.addSeparator()
        self.a_compare = tb.addAction("Compare original", self.refresh_page)
        self.a_compare.setCheckable(True)
        self.a_diff = tb.addAction("Show differences", self.refresh_page)
        self.a_diff.setCheckable(True)
        tb.addSeparator()
        pages_btn = QToolButton()
        pages_btn.setText("Pages")
        pages_btn.setPopupMode(QToolButton.InstantPopup)
        pm = QMenu(pages_btn)
        pm.addAction("Insert pages from another PDF…", self.insert_pages)
        pm.addAction("Duplicate this page", lambda: self._add(O.new_op("duplicate_page", page=self.page)))
        pm.addAction("Rotate left", lambda: self._rotate(270))
        pm.addAction("Rotate right", lambda: self._rotate(90))
        pm.addAction("Move page up", lambda: self._move_page(-1))
        pm.addAction("Move page down", lambda: self._move_page(1))
        pm.addAction("Delete this page…", self.delete_page)
        pm.addSeparator()
        pm.addAction("Change page size…", self.resize_page)
        pm.addAction("Split / extract pages to a new PDF…", self.extract_pages)
        pages_btn.setMenu(pm)
        tb.addWidget(pages_btn)
        doc_btn = QToolButton()
        doc_btn.setText("Document")
        doc_btn.setPopupMode(QToolButton.InstantPopup)
        dm = QMenu(doc_btn)
        dm.addAction("Page numbers", lambda: self._add_and_select(O.new_op("page_numbers")))
        dm.addAction("Header / footer", lambda: self._add_and_select(O.new_op("header_footer", footer="{filename}")))
        dm.addAction("Text watermark", lambda: self._add_and_select(O.new_op("watermark")))
        dm.addAction("Image watermark / logo on every page…", lambda: self._image_op(
            pymupdf.Rect(36, 36, 136, 86), all_pages=True))
        dm.addSeparator()
        dm.addAction("Document properties (title, author…)…", self.edit_metadata)
        dm.addAction("Add bookmark for this page…", self.add_bookmark)
        dm.addAction("Remove bookmark…", self.remove_bookmark)
        dm.addAction("Fill form fields…", self.fill_forms)
        dm.addSeparator()
        dm.addAction("Redact search terms…", self.redact_terms)
        dm.addAction("OCR this page…", self.ocr_page)
        dm.addAction("Internal PDF dates (advanced)…", lambda: self._add_and_select(O.new_op("set_metadata_dates")))
        doc_btn.setMenu(dm)
        tb.addWidget(doc_btn)
        lay.addWidget(tb)

        tools = QToolBar()
        tools.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.tool_group = QActionGroup(self)
        self.tool_actions = {}
        for key, label, tip in TOOLS:
            a = QAction(label, self)
            a.setCheckable(True)
            a.setToolTip(tip)
            a.triggered.connect(lambda _=False, k=key: self.set_tool(k))
            self.tool_group.addAction(a)
            tools.addAction(a)
            self.tool_actions[key] = a
        self.tool_actions["select"].setChecked(True)
        lay.addWidget(tools)

        sr = QHBoxLayout()
        sr.setContentsMargins(6, 0, 6, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find text (Ctrl+F)")
        self.search.returnPressed.connect(self.do_search)
        self.match_case = QCheckBox("Match case")
        self.hit_label = QLabel("")
        prev_b = QPushButton("◀")
        prev_b.clicked.connect(lambda: self.goto_hit(self.hit_idx - 1))
        next_b = QPushButton("▶")
        next_b.clicked.connect(lambda: self.goto_hit(self.hit_idx + 1))
        self.replace_with = QLineEdit()
        self.replace_with.setPlaceholderText("Replace with")
        rep_b = QPushButton("Replace all")
        rep_b.setToolTip("Genuine replacement in every match (original glyphs removed)")
        rep_b.clicked.connect(self.replace_all)
        red_b = QPushButton("Redact all…")
        red_b.clicked.connect(lambda: self.redact_terms(self.search.text()))
        for w in (self.search, self.match_case, prev_b, next_b, self.hit_label, self.replace_with, rep_b, red_b):
            sr.addWidget(w)
        sr.setStretch(0, 2)
        sr.setStretch(5, 1)
        lay.addLayout(sr)
        a = QAction(self)
        a.setShortcut(QKeySequence.Find)
        a.triggered.connect(lambda: (self.search.setFocus(), self.search.selectAll()))
        self.addAction(a)
        for seq, fn in ((QKeySequence.MoveToNextPage, lambda: self.goto_page(self.page + 1)),
                        (QKeySequence.MoveToPreviousPage, lambda: self.goto_page(self.page - 1))):
            a = QAction(self)
            a.setShortcut(seq)
            a.triggered.connect(fn)
            self.addAction(a)

        split = QSplitter()
        self.thumbs = QListWidget()
        self.thumbs.setViewMode(QListWidget.IconMode)
        self.thumbs.setFlow(QListWidget.TopToBottom)
        self.thumbs.setIconSize(QSize(110, 140))
        self.thumbs.setMovement(QListWidget.Static)
        self.thumbs.setFixedWidth(150)
        self.thumbs.currentRowChanged.connect(lambda r: r >= 0 and r != self.page and self.goto_page(r))
        self.thumbs.setContextMenuPolicy(Qt.CustomContextMenu)
        self.thumbs.customContextMenuRequested.connect(lambda pos: pm.exec(self.thumbs.mapToGlobal(pos)))
        split.addWidget(self.thumbs)

        center = QSplitter()
        self.orig_view = PageView(interactive=False)
        self.view = PageView()
        self.view.createRequested.connect(self.on_create)
        self.view.objectSelected.connect(self.on_canvas_select)
        self.view.objectGeometryChanged.connect(self.on_geometry)
        self.view.zoomRequested.connect(self.step_zoom)
        self.view.deleteRequested.connect(self.remove_op)
        o_box = QWidget()
        ol = QVBoxLayout(o_box)
        ol.setContentsMargins(0, 0, 0, 0)
        self.orig_label = QLabel("Original")
        ol.addWidget(self.orig_label)
        ol.addWidget(self.orig_view)
        m_box = QWidget()
        ml = QVBoxLayout(m_box)
        ml.setContentsMargins(0, 0, 0, 0)
        self.mod_label = QLabel("Result (final output preview)")
        ml.addWidget(self.mod_label)
        ml.addWidget(self.view)
        self.orig_box = o_box
        center.addWidget(o_box)
        center.addWidget(m_box)
        o_box.hide()
        split.addWidget(center)

        self.side = QTabWidget()
        self.props = PropertiesPanel()
        self.props.changed.connect(self.on_props_changed)
        self.props.assetRequested.connect(self.on_asset_requested)
        self.side.addTab(self.props, "Properties")
        self.side.addTab(self._ops_tab(), "Edits")
        self.doc_panel = QLabel()
        self.doc_panel.setWordWrap(True)
        self.doc_panel.setAlignment(Qt.AlignTop)
        self.doc_panel.setTextFormat(Qt.RichText)
        from PySide6.QtWidgets import QScrollArea
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setWidget(self.doc_panel)
        self.side.addTab(sa, "Document")
        self.side.addTab(self._assistant_tab(), "Assistant")
        self.side.setMinimumWidth(330)
        split.addWidget(self.side)
        split.setStretchFactor(1, 1)
        split.setSizes([150, 1000, 340])
        lay.addWidget(split, 1)
        self.status = QLabel("Open a PDF to start editing. Nothing you do here changes the original file.")
        self.status.setStyleSheet("padding:4px 8px;")
        lay.addWidget(self.status)

    def _ops_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.ops_list = QListWidget()
        self.ops_list.currentRowChanged.connect(self.on_ops_row)
        self.ops_list.itemChanged.connect(self.on_ops_item_changed)
        v.addWidget(self.ops_list)
        row = QHBoxLayout()
        for label, fn in (("Up", lambda: self._move_op(-1)), ("Down", lambda: self._move_op(1)),
                          ("Duplicate", self._dup_op), ("Delete", lambda: self.selected and self.remove_op(self.selected))):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        v.addLayout(row)
        v.addWidget(QLabel("<small>Edits are applied top to bottom on every matching download. "
                           "Untick to disable an edit without deleting it.</small>"))
        return w

    def _assistant_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(info_box("Describe changes in plain language. The local assistant (no internet, no paid AI) "
                             "proposes edits; nothing is applied until you tick and add them.<br>Examples: "
                             "<i>replace \"ACME\" with \"Globex\"; add page numbers bottom right; delete page 3; "
                             "highlight \"Total\"; redact \"secret\"; watermark \"DRAFT\"</i>", "info"))
        self.nl_input = QPlainTextEdit()
        self.nl_input.setFixedHeight(80)
        v.addWidget(self.nl_input)
        b = QPushButton("Propose edits")
        b.clicked.connect(self.nl_propose)
        v.addWidget(b)
        self.nl_list = QListWidget()
        v.addWidget(self.nl_list, 1)
        self.nl_notes = QLabel()
        self.nl_notes.setWordWrap(True)
        self.nl_notes.setStyleSheet("color:#b26a00;")
        v.addWidget(self.nl_notes)
        b2 = QPushButton("Add ticked edits")
        b2.clicked.connect(self.nl_accept)
        v.addWidget(b2)
        self._nl_ops = []
        return w

    # ================================================================== loading
    def _new_session(self, data: bytes, name: str, ops=None, profile_id=None):
        try:
            self.session = EditSession(data, ops, base_name=name, asset_resolver=self.repo.asset_resolver,
                                       session_dir=self.service.paths.sessions, profile_id=profile_id)
        except ValueError as e:
            error(self, str(e))
            return False
        except Exception:
            error(self, "This file could not be opened as a PDF.")
            return False
        self.profile_id = profile_id
        self.page, self.selected, self.hits, self._thumb_rev = 0, None, [], -1
        self.session.dirty = False
        info = inspect_doc(self.session.original())
        notes = []
        if info.signed:
            notes.append("This PDF is digitally signed. Any edit will invalidate the signature.")
        if info.scanned_pages:
            notes.append(f"{len(info.scanned_pages)} page(s) are scanned images: text cannot be edited directly. "
                         "Use Document ▸ OCR to add a searchable, correctable text layer, or add new text on top.")
        if info.encrypted:
            notes.append("The PDF is encrypted; the edited output will not carry the original restrictions.")
        if info.has_javascript:
            notes.append("Contains JavaScript; it is never executed here.")
        if notes:
            QMessageBox.information(self, "About this PDF", "\n\n".join(notes))
        self.refresh_all()
        self._update_enabled()
        return True

    def open_pdf_dialog(self):
        if not self._maybe_discard():
            return
        fn, _ = QFileDialog.getOpenFileName(self, "Open PDF to edit", "", "PDF files (*.pdf)")
        if fn:
            self.open_file(fn)

    def open_file(self, path, profile_id=None, ops=None):
        data = Path(path).read_bytes()
        return self._new_session(data, Path(path).name, ops, profile_id)

    def _fill_profile_menu(self):
        self.profile_menu.clear()
        profiles = self.repo.list_profiles()
        if not profiles:
            self.profile_menu.addAction("(no profiles yet)").setEnabled(False)
        for p in profiles:
            self.profile_menu.addAction(f"{p.name}  ({p.mode}, {len(p.operations)} edits)",
                                        lambda pid=p.id: self.open_profile(pid))
        saves = EditSession.list_autosaves(self.service.paths.sessions)
        if saves:
            self.profile_menu.addSeparator()
            sub = self.profile_menu.addMenu("Recover autosaved session")
            for s in saves[:10]:
                from .common import fmt_short
                sub.addAction(f"{s['name']} — {s['ops']} edits — {fmt_short(s['saved_ns'])}",
                              lambda p=s["path"]: self.recover_session(p))

    def open_profile(self, profile_id: int):
        if not self._maybe_discard():
            return
        prof = self.repo.get_profile(profile_id)
        try:
            fid = prof.base_file_id or (prof.master_file_id if not prof.operations else None)
            if fid:
                path = self.repo.file_path(fid)
            else:
                fn, _ = QFileDialog.getOpenFileName(self, f"Choose a sample of '{prof.name}' to design the edits on",
                                                    "", "PDF files (*.pdf)")
                if not fn:
                    return
                path = Path(fn)
        except PRMError as e:
            error(self, e.user_message)
            return
        if self._new_session(path.read_bytes(), path.name, prof.operations, prof.id):
            self.statusMessage.emit(f"Editing profile '{prof.name}' (version {prof.version_no})")

    def recover_session(self, path):
        if not self._maybe_discard():
            return
        try:
            s = EditSession.load_autosave(path, asset_resolver=self.repo.asset_resolver)
        except Exception as e:
            error(self, f"Autosave could not be restored: {e}")
            return
        self.session, self.profile_id = s, s.profile_id
        self.page, self.selected, self._thumb_rev = 0, None, -1
        self.refresh_all()
        self._update_enabled()

    def _maybe_discard(self) -> bool:
        if self.session and self.session.dirty:
            r = QMessageBox.question(self, "Unsaved edits",
                                     "You have edits that are not saved to a profile. They stay recoverable from "
                                     "'Open profile > Recover autosaved session'. Continue?",
                                     QMessageBox.Yes | QMessageBox.No)
            return r == QMessageBox.Yes
        return True

    # ================================================================== refresh
    def refresh_all(self):
        self.refresh_page()
        self.refresh_ops()
        self.refresh_doc_panel()

    def refresh_page(self):
        s = self.session
        if not s:
            return
        doc = s.document()
        self.page = max(0, min(self.page, doc.page_count - 1))
        if self._thumb_rev != s.revision:
            self._refresh_thumbs()
        page = doc[self.page]
        self.view.show_page(page, self.zoom, s.objects_on_page(self.page), self.selected)
        if self.hits:
            on_page = [(i, h[1]) for i, h in enumerate(self.hits) if h[0] == self.page]
            cur = next((k for k, (i, _r) in enumerate(on_page) if i == self.hit_idx), -1)
            self.view.set_highlights([r for _i, r in on_page], cur)
        self.view.set_tool(self.view.tool)
        compare = self.a_compare.isChecked() or self.a_diff.isChecked()
        self.orig_box.setVisible(compare)
        if compare:
            o = s.origin_of_final(self.page)
            if o is None:
                self.orig_label.setText("Original: (this page was inserted)")
                self.orig_view.scene().clear()
            else:
                self.orig_label.setText(f"Original page {o + 1}")
                self.orig_view.show_page(s.original()[o], self.zoom)
                if self.a_diff.isChecked():
                    self._overlay_diff(s.original()[o], page)
        self.thumbs.blockSignals(True)
        self.thumbs.setCurrentRow(self.page)
        self.thumbs.blockSignals(False)
        res = s.results()
        failed = [r for r in res.values() if not r.ok]
        name = self.repo.get_profile(self.profile_id).name if self.profile_id else "not saved to a profile"
        self.status.setText(f"{s.base_name} — page {self.page + 1} of {doc.page_count} — profile: {name}"
                            f"{' — unsaved changes (autosaved)' if s.dirty else ''}"
                            + (f" — ⚠ {len(failed)} edit(s) failed, see Edits" if failed else ""))
        self.status.setStyleSheet("padding:4px 8px;" + ("color:#c62828;" if failed else ""))
        self.a_undo.setEnabled(s.can_undo())
        self.a_redo.setEnabled(s.can_redo())

    def _overlay_diff(self, orig_page, page):
        a = orig_page.get_pixmap(matrix=pymupdf.Matrix(self.zoom, self.zoom), alpha=False)
        b = page.get_pixmap(matrix=pymupdf.Matrix(self.zoom, self.zoom), alpha=False)
        if (a.width, a.height) != (b.width, b.height):
            self.mod_label.setText("Result — page size or rotation changed; pixel difference not shown")
            return
        from PIL import Image, ImageChops
        ia = Image.frombytes("RGB", (a.width, a.height), a.samples)
        ib = Image.frombytes("RGB", (b.width, b.height), b.samples)
        mask = ImageChops.difference(ia, ib).convert("L").point(lambda v: 255 if v > 24 else 0)
        overlay = Image.new("RGBA", ia.size, (229, 57, 53, 0))
        overlay.putalpha(mask.point(lambda v: 110 if v else 0))
        data = overlay.tobytes("raw", "RGBA")
        img = QImage(data, overlay.width, overlay.height, QImage.Format_RGBA8888).copy()
        it = self.view.scene().addPixmap(QPixmap.fromImage(img))
        it.setZValue(4)
        self.mod_label.setText("Result — changed pixels shown in red")

    def _refresh_thumbs(self):
        s = self.session
        doc = s.document()
        self._thumb_rev = s.revision
        self.thumbs.blockSignals(True)
        self.thumbs.clear()
        for i, page in enumerate(doc):
            if i >= 500:
                break
            z = 110 / max(page.rect.width, page.rect.height) * 1.3
            pm = render_page(page, z)
            it = QListWidgetItem(QIcon(pm), str(i + 1))
            self.thumbs.addItem(it)
        self.thumbs.blockSignals(False)

    def refresh_ops(self):
        s = self.session
        self.ops_list.blockSignals(True)
        self.ops_list.clear()
        if s:
            res = s.results()
            for op in s.ops:
                spec = O.REGISTRY[op["type"]]
                r = res.get(op["id"])
                mark = "✗ " if r and not r.ok else "⚠ " if r and r.warnings else ""
                eff = EFFECT_TEXT.get(spec.effect, (spec.effect,))[0]
                it = QListWidgetItem(f"{mark}{O.describe(op)}   [{eff}]")
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked if op.get("enabled", True) else Qt.Unchecked)
                it.setData(Qt.UserRole, op["id"])
                if r and not r.ok:
                    it.setForeground(QColor("#c62828"))
                    it.setToolTip(r.error)
                elif r and r.warnings:
                    it.setToolTip("\n".join(r.warnings))
                self.ops_list.addItem(it)
                if op["id"] == self.selected:
                    self.ops_list.setCurrentItem(it)
        self.ops_list.blockSignals(False)
        self._show_props()

    def refresh_doc_panel(self):
        s = self.session
        if not s:
            self.doc_panel.setText("")
            return
        d = s.document()
        info = inspect_doc(s.original())
        meta = s.original().metadata or {}
        fields = [(w.field_name, w.field_type_string, w.field_value) for p in d for w in (p.widgets() or [])]
        toc = d.get_toc()
        html = [f"<h3>{s.base_name}</h3>",
                f"Original: {s.base_pages} page(s) &nbsp;→&nbsp; Result: {d.page_count} page(s)<br>"]
        if info.warnings:
            html.append("<p style='color:#b26a00'>" + "<br>".join("⚠ " + w for w in info.warnings) + "</p>")
        html.append("<h4>Dates — three different things</h4><table cellspacing=4>"
                    f"<tr><td><b>PDF internal CreationDate</b></td><td>{meta.get('creationDate') or '—'}</td></tr>"
                    f"<tr><td><b>PDF internal ModDate</b></td><td>{meta.get('modDate') or '—'}</td></tr></table>"
                    "<small>Internal dates are preserved unless you add the explicit 'Internal PDF dates' edit. "
                    "Filesystem timestamps of generated files are set by the profile's timestamp policy. "
                    "Dates printed on pages only change through text edits you make.</small>")
        html.append("<h4>Properties</h4>" + "".join(f"{k}: {meta.get(k) or '—'}<br>" for k in
                                                     ("title", "author", "subject", "keywords", "producer")))
        html.append(f"<h4>Bookmarks ({len(toc)})</h4>" + "".join(
            f"{'&nbsp;' * 3 * (lvl - 1)}{t} → p.{pg}<br>" for lvl, t, pg in toc[:40]))
        html.append(f"<h4>Form fields ({len(fields)})</h4>" + "".join(
            f"{n} <i>({t})</i>: {v}<br>" for n, t, v in fields[:60]))
        self.doc_panel.setText("".join(html))

    def _show_props(self):
        s = self.session
        op = s.find(self.selected) if s and self.selected else None
        self.props.set_op(op, s.page_count if s else 1, s.results().get(self.selected) if op else None)
        if op:
            self.side.setCurrentWidget(self.props)

    def _update_enabled(self):
        on = self.session is not None
        for a in (self.a_save, self.a_save_as, self.a_master, self.a_export, self.a_undo, self.a_redo):
            a.setEnabled(on)

    # ================================================================== navigation
    def goto_page(self, p):
        if self.session and 0 <= p < self.session.page_count:
            self.page = p
            self.refresh_page()

    def set_zoom(self, z):
        self.zoom = z
        self.refresh_page()

    def step_zoom(self, d):
        i = min(range(len(ZOOMS)), key=lambda k: abs(ZOOMS[k] - self.zoom))
        i = max(0, min(len(ZOOMS) - 1, i + d))
        self.zoom_box.setCurrentIndex(i)

    def fit_width(self):
        if self.session:
            w = self.session.document()[self.page].rect.width
            z = max(0.25, min(4.0, (self.view.viewport().width() - 40) / w))
            self.zoom_box.blockSignals(True)
            self.zoom_box.setCurrentIndex(min(range(len(ZOOMS)), key=lambda k: abs(ZOOMS[k] - z)))
            self.zoom_box.blockSignals(False)
            self.set_zoom(z)

    def set_tool(self, key):
        self.view.set_tool(key)
        self.tool_actions[key].setChecked(True)

    # ================================================================== edits
    def _add(self, op, select=False) -> bool:
        if not self.session:
            return False
        errs = self.session.add(op)
        if errs:
            QMessageBox.warning(self, "Edit not added", "\n".join(errs))
            return False
        r = self.session.results().get(op["id"])
        if select:
            self.selected = op["id"]
        self.refresh_all()
        if r and not r.ok:
            QMessageBox.warning(self, "Edit could not be applied", r.error or "Failed")
        return True

    def _add_and_select(self, op):
        if self._add(op, select=True):
            self.side.setCurrentWidget(self.props)

    def remove_op(self, op_id):
        if self.session and op_id:
            self.session.remove(op_id)
            if self.selected == op_id:
                self.selected = None
            self.refresh_all()

    def _move_op(self, d):
        if self.session and self.selected:
            self.session.move(self.selected, d)
            self.refresh_all()

    def _dup_op(self):
        if self.session and self.selected:
            import copy
            import uuid
            op = copy.deepcopy(self.session.find(self.selected))
            op["id"] = uuid.uuid4().hex[:12]
            if "rect" in op and op.get("rect"):
                op["rect"] = [op["rect"][0] + 12, op["rect"][1] + 12, op["rect"][2] + 12, op["rect"][3] + 12]
            self._add(op, select=True)

    def undo(self):
        if self.session:
            self.session.undo()
            self.selected = self.selected if self.selected and self.session.find(self.selected) else None
            self.refresh_all()

    def redo(self):
        if self.session:
            self.session.redo()
            self.refresh_all()

    def on_props_changed(self, op_id, changes):
        errs = self.session.update(op_id, **changes)
        if errs:
            QMessageBox.warning(self, "Invalid value", "\n".join(errs))
        self.refresh_all()

    def on_ops_row(self, row):
        if row < 0 or not self.session:
            return
        op_id = self.ops_list.item(row).data(Qt.UserRole)
        self.selected = op_id
        op = self.session.find(op_id)
        if isinstance(op.get("page"), int):
            i = self.session.ops.index(op)
            fp = self.session.final_page_of(i, op["page"])
            if fp is not None and fp != self.page:
                self.page = fp
        self.refresh_page()
        self._show_props()

    def on_ops_item_changed(self, item):
        op_id = item.data(Qt.UserRole)
        en = item.checkState() == Qt.Checked
        if self.session.find(op_id).get("enabled", True) != en:
            self.session.update(op_id, enabled=en)
            self.refresh_all()

    def on_canvas_select(self, op_id):
        self.selected = op_id
        self.ops_list.blockSignals(True)
        for i in range(self.ops_list.count()):
            if self.ops_list.item(i).data(Qt.UserRole) == op_id:
                self.ops_list.setCurrentRow(i)
        self.ops_list.blockSignals(False)
        self._show_props()

    def on_geometry(self, op_id, page_rect):
        media = self.session.page_rect_to_media(self.page, page_rect)
        errs = self.session.update(op_id, rect=media)
        if errs:
            QMessageBox.warning(self, "Cannot move here", "\n".join(errs))
        self.selected = op_id
        QTimer.singleShot(0, self.refresh_all)

    def on_asset_requested(self, op_id, field):
        op = self.session.find(op_id)
        flt = "PDF files (*.pdf)" if op["type"] == "insert_pages" else "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp)"
        fn, _ = QFileDialog.getOpenFileName(self, "Choose file", "", flt)
        if fn:
            fid = self.repo.store_file(fn, "asset")
            sha = self.repo.file_row(fid)["sha256"]
            self.session.update(op_id, **{field: sha})
            self.refresh_all()

    # ================================================================== canvas creation
    def on_create(self, tool, rect: pymupdf.Rect, extra):
        if not self.session:
            return
        p = self.page
        page = self.session.document()[p]
        media = lambda r: self.session.page_rect_to_media(p, r)  # noqa: E731
        click = extra.get("click") if isinstance(extra, dict) else False
        if tool in ("text", "comment") and click:
            rect = pymupdf.Rect(rect.x0, rect.y0, rect.x0 + 200, rect.y0 + (32 if tool == "text" else 50))
        if tool == "text":
            self._add_and_select(O.new_op("add_text", page=p, rect=media(rect), text="Text"))
            self.set_tool("select")
        elif tool == "comment":
            self._add_and_select(O.new_op("comment", page=p, rect=media(rect), text="Comment"))
            self.set_tool("select")
        elif tool == "note":
            pt = extra["point"]
            self._add_and_select(O.new_op("note", page=p, rect=media(pymupdf.Rect(pt.x, pt.y, pt.x + 20, pt.y + 20)),
                                          text=""))
        elif tool in ("rect", "ellipse") and not click:
            self._add_and_select(O.new_op("shape", page=p, kind=tool, rect=media(rect)))
        elif tool in ("line", "arrow"):
            self._add_and_select(O.new_op("shape", page=p, kind=tool, rect=media(rect), points=extra["points"]))
        elif tool == "ink":
            self._add_and_select(O.new_op("shape", page=p, kind="ink", rect=media(rect), strokes=extra["strokes"]))
        elif tool == "image":
            if click:
                rect = pymupdf.Rect(rect.x0, rect.y0, rect.x0 + 150, rect.y0 + 100)
            self._image_op(rect)
        elif tool in ("highlight", "underline", "strikeout"):
            ws = words_in_rect(page, rect) if not click else [w for w in [word_at(page, extra["point"])] if w]
            if not ws:
                self.statusMessage.emit("No text under the selection.")
                return
            self._add(O.new_op("markup", kind=tool, page=p, rects=[media(w) for w in ws]))
        elif tool == "redact" and not click:
            if not self._redact_explained:
                if not confirm(self, "Secure redaction",
                               "Redaction PERMANENTLY removes the text, image pixels and drawings inside the area "
                               "from the generated output (it is not just a black box on top).\n\nReview the "
                               "preview before saving. You can still remove this edit from the Edits list.",
                               ok_text="Add redaction"):
                    return
                self._redact_explained = True
            self._add(O.new_op("redact", areas=[{"page": p, "rect": media(rect)}]), select=True)
        elif tool == "field" and not click:
            self._add_field(p, media(rect))
        elif tool == "crop" and not click:
            if confirm(self, "Crop page", f"Crop page {p + 1} to the selected area? Content outside the area is "
                                          "hidden (not deleted)."):
                self._add(O.new_op("crop_pages", pages=str(p + 1), rect=media(rect)))
                self.set_tool("select")
        elif tool == "edit_text":
            if click:
                w = word_at(page, extra["point"])
                if not w:
                    self.statusMessage.emit("Click on a word, or drag across text on one line.")
                    return
                rect = w
            text, bbox = selected_line_text(page, rect)
            if text is None:
                QMessageBox.information(self, "Edit text", bbox)
                return
            self._edit_text_dialog(p, text, bbox)

    def _image_op(self, rect, all_pages=False):
        fn, _ = QFileDialog.getOpenFileName(self, "Choose image", "", "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp)")
        if not fn:
            return
        fid = self.repo.store_file(fn, "asset")
        sha = self.repo.file_row(fid)["sha256"]
        self._add_and_select(O.new_op("add_image", page=self.page, rect=self.session.page_rect_to_media(
            self.page, rect), asset=sha, all_pages=all_pages))
        self.set_tool("select")

    def _edit_text_dialog(self, p, text, bbox):
        dlg = QDialog(self)
        dlg.setWindowTitle("Edit existing text")
        f = QFormLayout(dlg)
        f.addRow("Current text", QLabel(f"<b>{text}</b>"))
        new = QLineEdit(text)
        f.addRow("Replace with", new)
        rb_rep = QRadioButton("Replace (genuine: the old glyphs are removed from the page)")
        rb_del = QRadioButton("Delete (genuine removal; the space stays blank)")
        rb_rep.setChecked(True)
        f.addRow(rb_rep)
        f.addRow(rb_del)
        every = QCheckBox("Apply to every occurrence in the document (not just this spot)")
        f.addRow(every)
        f.addRow(info_box("The original font is reused when it is embedded and contains every needed character; "
                          "otherwise a close standard font is used and you will be told. Check the result in the "
                          "preview.", "info"))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        new.selectAll()
        if dlg.exec() != QDialog.Accepted:
            return
        target = {} if every.isChecked() else {"page": p, "area": self.session.page_rect_to_media(p, bbox)}
        if rb_del.isChecked() or not new.text():
            op = O.new_op("delete_text", find=text, match_case=True, **target)
        else:
            op = O.new_op("replace_text", find=text, replace=new.text(), match_case=True, **target)
        self._add(op, select=True)

    def _add_field(self, p, media_rect):
        dlg = QDialog(self)
        dlg.setWindowTitle("New form field")
        f = QFormLayout(dlg)
        name = QLineEdit()
        kind = QComboBox()
        kind.addItems(["text", "checkbox", "combobox", "listbox"])
        choices = QLineEdit()
        choices.setPlaceholderText("for lists: a, b, c")
        f.addRow("Name", name)
        f.addRow("Type", kind)
        f.addRow("Choices", choices)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if dlg.exec() == QDialog.Accepted and name.text().strip():
            self._add(O.new_op("add_field", page=p, rect=media_rect, name=name.text().strip(),
                               kind=kind.currentText(), choices=choices.text()), select=True)

    # ================================================================== page / document actions
    def _rotate(self, angle):
        self._add(O.new_op("rotate_pages", pages=str(self.page + 1), angle=angle))

    def _move_page(self, d):
        t = self.page + d
        if self.session and 0 <= t < self.session.page_count:
            if self._add(O.new_op("move_page", page=self.page, to=t)):
                self.goto_page(t)

    def delete_page(self):
        if self.session and confirm(self, "Delete page", f"Delete page {self.page + 1}?", "Delete", danger=True):
            self._add(O.new_op("delete_pages", pages=str(self.page + 1)))

    def insert_pages(self):
        if not self.session:
            return
        fn, _ = QFileDialog.getOpenFileName(self, "Insert pages from", "", "PDF files (*.pdf)")
        if not fn:
            return
        try:
            with pymupdf.open(fn) as src:
                if src.needs_pass:
                    raise ValueError("password protected")
                n = src.page_count
        except Exception:
            error(self, "That PDF cannot be read (or is password protected).")
            return
        spec, ok = QInputDialog.getText(self, "Insert pages", f"Which pages of that file ({n} pages)?", text="all")
        if not ok:
            return
        try:
            count = len(O.parse_pages(spec, n))
        except PRMError as e:
            error(self, e.user_message)
            return
        fid = self.repo.store_file(fn, "asset")
        self._add(O.new_op("insert_pages", asset=self.repo.file_row(fid)["sha256"], from_pages=spec,
                           at=self.page + 1, count=count))

    def resize_page(self):
        sizes = {"A4 portrait (595×842)": (595, 842), "A4 landscape": (842, 595), "Letter portrait (612×792)":
                 (612, 792), "Letter landscape": (792, 612), "Legal (612×1008)": (612, 1008)}
        k, ok = QInputDialog.getItem(self, "Page size", "New size for this page (content is not scaled):",
                                     list(sizes), 0, False)
        if ok:
            w, h = sizes[k]
            self._add(O.new_op("resize_pages", pages=str(self.page + 1), width=w, height=h))

    def extract_pages(self):
        if not self.session:
            return
        spec, ok = QInputDialog.getText(self, "Split / extract", "Pages to save as a new PDF (e.g. 1-3, 5):",
                                        text=str(self.page + 1))
        if not ok:
            return
        try:
            pages = O.parse_pages(spec, self.session.page_count)
            fn, _ = QFileDialog.getSaveFileName(self, "Save extracted pages", "", "PDF files (*.pdf)")
            if fn:
                self.session.export_pages(fn, pages)
                self.statusMessage.emit(f"Saved {len(pages)} page(s) to {fn}")
        except (PRMError, ValueError) as e:
            error(self, getattr(e, "user_message", str(e)))

    def edit_metadata(self):
        if not self.session:
            return
        meta = self.session.document().metadata or {}
        dlg = QDialog(self)
        dlg.setWindowTitle("Document properties")
        f = QFormLayout(dlg)
        edits = {}
        for k in ("title", "author", "subject", "keywords"):
            edits[k] = QLineEdit(meta.get(k, ""))
            f.addRow(k.title(), edits[k])
        f.addRow(info_box("Creation/modification dates inside the PDF are kept as they are.", "info"))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if dlg.exec() == QDialog.Accepted:
            changes = {k: e.text() for k, e in edits.items() if e.text() != meta.get(k, "")}
            if changes:
                self._add(O.new_op("set_metadata", **changes))

    def add_bookmark(self):
        if self.session:
            t, ok = QInputDialog.getText(self, "Add bookmark", f"Title for page {self.page + 1}:")
            if ok and t.strip():
                self._add(O.new_op("add_bookmark", title=t.strip(), page=self.page))

    def remove_bookmark(self):
        if not self.session:
            return
        toc = self.session.document().get_toc()
        if not toc:
            QMessageBox.information(self, "Bookmarks", "This document has no bookmarks.")
            return
        t, ok = QInputDialog.getItem(self, "Remove bookmark", "Bookmark (and its children):",
                                     [x[1] for x in toc], 0, False)
        if ok:
            self._add(O.new_op("remove_bookmark", title=t))

    def fill_forms(self):
        if not self.session:
            return
        doc = self.session.document()
        widgets = [(w.field_name, w.field_type_string, w.field_value) for p in doc for w in (p.widgets() or [])
                   if w.field_type != pymupdf.PDF_WIDGET_TYPE_SIGNATURE]
        if not widgets:
            QMessageBox.information(self, "Forms", "No fillable form fields. Use the 'Form field' tool to add some.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Fill form fields")
        v = QVBoxLayout(dlg)
        t = QTableWidget(len(widgets), 3)
        t.setHorizontalHeaderLabels(["Field", "Type", "Value"])
        for i, (n, ty, val) in enumerate(widgets):
            for c, x in enumerate((n, ty)):
                it = QTableWidgetItem(str(x))
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                t.setItem(i, c, it)
            t.setItem(i, 2, QTableWidgetItem("" if val in (None, False) else str(val)))
        v.addWidget(t)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        dlg.resize(560, 400)
        if dlg.exec() == QDialog.Accepted:
            ops = [O.new_op("fill_field", name=n, value=t.item(i, 2).text()) for i, (n, ty, val) in enumerate(widgets)
                   if t.item(i, 2).text() != ("" if val in (None, False) else str(val))]
            if ops:
                errs = self.session.add_many(ops)
                if errs:
                    QMessageBox.warning(self, "Forms", "\n".join(errs))
                self.refresh_all()

    def redact_terms(self, preset: str = ""):
        if not self.session:
            return
        text, ok = QInputDialog.getText(self, "Redact search terms",
                                        "Terms to remove permanently (separate with |):", text=preset or "")
        terms = [x.strip() for x in text.split("|") if x.strip()] if ok else []
        if not terms:
            return
        hits = sum(len(find_text(p, t)) for p in self.session.document() for t in terms)
        if confirm(self, "Review redaction", f"{hits} match(es) will be permanently removed and blacked out on the "
                                             "output. Review the preview afterwards.", ok_text="Add redaction",
                   danger=True):
            self._add(O.new_op("redact", terms=terms), select=True)

    def ocr_page(self):
        if not self.session:
            return
        page = self.session.document()[self.page]
        try:
            tp = page.get_textpage_ocr(full=True, dpi=300)
            words = page.get_text("words", textpage=tp)
        except Exception:
            QMessageBox.information(self, "OCR not available",
                                    "OCR needs the free Tesseract OCR engine, which is not installed.\n\n"
                                    "Install it (e.g. from the UB Mannheim Windows build), make sure the "
                                    "TESSDATA_PREFIX environment variable points to its 'tessdata' folder, and "
                                    "restart. You can still add a text layer manually via Edits.\n\n"
                                    "OCR accuracy depends on scan quality and language.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("OCR result — correct before adding")
        v = QVBoxLayout(dlg)
        v.addWidget(info_box("Correct any misread words. The corrected words are added as an invisible, searchable "
                             "text layer; the page image is unchanged. Accuracy depends on scan quality and "
                             "language.", "info"))
        t = QTableWidget(len(words), 1)
        t.setHorizontalHeaderLabels(["Word"])
        for i, w in enumerate(words):
            t.setItem(i, 0, QTableWidgetItem(w[4]))
        v.addWidget(t)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        if dlg.exec() == QDialog.Accepted:
            items = []
            for i, w in enumerate(words):
                m = self.session.page_rect_to_media(self.page, pymupdf.Rect(w[:4]))
                items.append([t.item(i, 0).text(), *m])
            self._add(O.new_op("ocr_text_layer", page=self.page, words=items))

    # ================================================================== search
    def do_search(self):
        if not self.session:
            return
        q = self.search.text()
        self.hits = []
        if q:
            for i, p in enumerate(self.session.document()):
                for h in find_text(p, q, match_case=self.match_case.isChecked()):
                    self.hits.append((i, h.rect))
        self.hit_label.setText(f"{len(self.hits)} found" if q else "")
        self.goto_hit(0) if self.hits else self.refresh_page()

    def goto_hit(self, i):
        if not self.hits:
            return
        self.hit_idx = i % len(self.hits)
        self.page = self.hits[self.hit_idx][0]
        self.hit_label.setText(f"{self.hit_idx + 1} of {len(self.hits)}")
        self.refresh_page()

    def replace_all(self):
        q = self.search.text()
        if not (self.session and q):
            return
        if not self.hits:
            self.do_search()
        if not self.hits:
            QMessageBox.information(self, "Replace", "Text not found.")
            return
        if confirm(self, "Replace all", f"Replace {len(self.hits)} occurrence(s) of “{q}” with "
                                        f"“{self.replace_with.text()}”? The original glyphs are removed."):
            self._add(O.new_op("replace_text", find=q, replace=self.replace_with.text(),
                               match_case=self.match_case.isChecked()), select=True)
            self.hits = []

    # ================================================================== assistant
    def nl_propose(self):
        n = self.session.page_count if self.session else 1
        self._nl_ops, notes = propose(self.nl_input.toPlainText(), n)
        self.nl_list.clear()
        for op in self._nl_ops:
            spec = O.REGISTRY[op["type"]]
            it = QListWidgetItem(f"{O.describe(op)}   [{EFFECT_TEXT.get(spec.effect, (spec.effect,))[0]}]")
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            self.nl_list.addItem(it)
        self.nl_notes.setText("\n".join(notes))

    def nl_accept(self):
        if not self.session:
            error(self, "Open a PDF first.")
            return
        chosen = [op for i, op in enumerate(self._nl_ops) if self.nl_list.item(i).checkState() == Qt.Checked]
        if chosen:
            errs = self.session.add_many(chosen)
            if errs:
                QMessageBox.warning(self, "Not added", "\n".join(errs))
            self.nl_list.clear()
            self._nl_ops = []
            self.refresh_all()

    # ================================================================== saving
    def _store_base(self) -> int:
        tmp = Path(tempfile.mkdtemp(dir=self.service.paths.root)) / self.session.base_name
        try:
            tmp.write_bytes(self.session.base_pdf)
            return self.repo.store_file(tmp, "reference")
        finally:
            shutil.rmtree(tmp.parent, ignore_errors=True)

    def save_to_profile(self, new: bool = False) -> bool:
        s = self.session
        if not s:
            return False
        failed = [r for r in s.results().values() if not r.ok]
        if failed and not confirm(self, "Some edits fail", f"{len(failed)} edit(s) currently fail on this document. "
                                                           "Save anyway?"):
            return False
        base_id = self._store_base()
        if self.profile_id and not new:
            prof = self.repo.get_profile(self.profile_id)
            prof.operations, prof.base_file_id = list(s.ops), base_id
            try:
                prof = self.repo.save_profile(prof, note="Edited in editor", validate=prof.enabled)
            except PRMError as e:
                error(self, e.user_message)
                return False
        else:
            from ..pdfinfo import fingerprint_file
            from ..repository import Profile
            prof = Profile(id=None, name="", mode="operations")
            prof.operations, prof.base_file_id = list(s.ops), base_id
            tmp = self.repo.file_path(base_id)
            prof.reference_fingerprint = fingerprint_file(tmp)
            dlg = ProfileDialog(self.service, prof, self, base_pdf=Path(s.base_name))
            dlg.prof.reference_fingerprint = prof.reference_fingerprint
            if dlg.exec() != QDialog.Accepted:
                return False
            prof = dlg.prof
        self.profile_id = prof.id
        s.profile_id = prof.id
        s.dirty = False
        s.autosave()
        self.refresh_page()
        self.statusMessage.emit(f"Saved {len(s.ops)} edit(s) to profile '{prof.name}' (version {prof.version_no}).")
        self.profileSaved.emit(prof.id)
        return True

    def save_as_master(self):
        s = self.session
        if not s:
            return
        try:
            data = s.output_bytes()
        except ValueError as e:
            error(self, str(e))
            return
        tmpdir = Path(tempfile.mkdtemp(dir=self.service.paths.root))
        try:
            f = tmpdir / s.base_name
            f.write_bytes(data)
            fid = self.repo.store_file(f, "master")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        base_id = self._store_base()
        if self.profile_id:
            prof = self.repo.get_profile(self.profile_id)
            use = confirm(self, "Save as master", f"Store this result as the saved master of '{prof.name}' and "
                                                  "switch the profile to master-replacement mode?\n\n(The edits are "
                                                  "kept too, so you can switch back.)")
            if not use:
                return
            prof.master_file_id, prof.mode, prof.operations, prof.base_file_id = fid, "master", list(s.ops), base_id
            try:
                prof = self.repo.save_profile(prof, note="Master saved from editor", validate=prof.enabled)
            except PRMError as e:
                error(self, e.user_message)
                return
        else:
            from ..repository import Profile
            from ..pdfinfo import fingerprint_file
            prof = Profile(id=None, name="", mode="master")
            prof.master_file_id, prof.operations, prof.base_file_id = fid, list(s.ops), base_id
            prof.reference_fingerprint = fingerprint_file(self.repo.file_path(base_id))
            dlg = ProfileDialog(self.service, prof, self, base_pdf=Path(s.base_name))
            dlg.prof.reference_fingerprint = prof.reference_fingerprint
            if dlg.exec() != QDialog.Accepted:
                return
            prof = dlg.prof
        self.profile_id = s.profile_id = prof.id
        s.dirty = False
        s.autosave()
        self.refresh_page()
        self.statusMessage.emit(f"Master saved for '{prof.name}'.")
        self.profileSaved.emit(prof.id)

    def export_pdf(self):
        s = self.session
        if not s:
            return
        fn, _ = QFileDialog.getSaveFileName(self, "Export edited PDF", str(Path.home() / f"{Path(s.base_name).stem}"
                                                                           f"-edited.pdf"), "PDF files (*.pdf)")
        if fn:
            try:
                s.export(fn)
                self.statusMessage.emit(f"Exported to {fn}")
            except ValueError as e:
                error(self, str(e))
