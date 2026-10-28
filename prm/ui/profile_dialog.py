"""Create / edit a saved modification profile."""
from __future__ import annotations

import copy
from pathlib import Path

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QRadioButton,
                               QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from ..errors import PRMError
from ..operations import describe
from ..pdfinfo import fingerprint_file, inspect_pdf
from ..repository import Profile
from ..timestamps import SOURCES, TimestampPolicy, iso_to_ns
from .common import confirm, fmt_ns, info_box

SOURCE_LABELS = {"download_modified": "Download's modified time", "download_created": "Download's creation time",
                 "detected": "Time the download was detected", "processing": "Time the output was generated",
                 "fixed": "A fixed date/time"}


def _lines(edit: QPlainTextEdit) -> list[str]:
    return [x.strip() for x in edit.toPlainText().splitlines() if x.strip()]


class ProfileDialog(QDialog):
    def __init__(self, service, profile: Profile | None = None, parent=None, *, base_pdf: Path | None = None):
        super().__init__(parent)
        self.service, self.repo = service, service.repo
        self.prof = copy.deepcopy(profile) if profile else Profile(id=None, name="")
        self.was_enabled = bool(profile and profile.enabled and profile.id)
        self.setWindowTitle("Edit profile" if self.prof.id else "New profile")
        self.resize(760, 640)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)
        tabs.addTab(self._general(), "General")
        tabs.addTab(self._matching(), "Matching")
        tabs.addTab(self._output(), "Output")
        tabs.addTab(self._timestamps(), "Timestamps")
        tabs.addTab(self._options(), "Safety")
        if self.prof.id:
            tabs.addTab(self._versions(), "Versions")
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        if base_pdf and not self.prof.rules.get("filenames"):
            self.filenames.setPlainText(Path(base_pdf).name)
            if not self.name.text():
                self.name.setText(Path(base_pdf).stem)
            if Path(base_pdf).is_file() and not self.prof.reference_fingerprint:
                self._set_reference(base_pdf)

    # ------------------------------------------------------------------ tabs
    def _general(self):
        w = QWidget()
        f = QFormLayout(w)
        self.name = QLineEdit(self.prof.name)
        f.addRow("Profile name", self.name)
        self.enabled = QCheckBox("Process matching downloads automatically")
        self.enabled.setChecked(self.prof.enabled)
        f.addRow("", self.enabled)
        self.priority = QSpinBox()
        self.priority.setRange(-100, 100)
        self.priority.setValue(self.prof.priority)
        self.priority.setToolTip("When several profiles match, the higher priority wins.")
        f.addRow("Priority", self.priority)
        box = QGroupBox("Modification method")
        bl = QVBoxLayout(box)
        self.mode_ops = QRadioButton("Apply saved edits to each new download (operations)")
        self.mode_master = QRadioButton("Replace with my saved, already-edited master PDF")
        (self.mode_master if self.prof.mode == "master" else self.mode_ops).setChecked(True)
        bl.addWidget(self.mode_ops)
        self.ops_label = QLabel()
        self.ops_label.setWordWrap(True)
        self.ops_label.setStyleSheet("margin-left:22px; color: palette(placeholder-text);")
        bl.addWidget(self.ops_label)
        bl.addWidget(self.mode_master)
        row = QHBoxLayout()
        self.master_label = QLabel()
        b = QPushButton("Import edited master PDF…")
        b.clicked.connect(self._import_master)
        row.addWidget(self.master_label, 1)
        row.addWidget(b)
        bl.addLayout(row)
        f.addRow(box)
        self._refresh_mode_labels()
        if self.prof.id:
            f.addRow("Profile created", QLabel(fmt_ns(self.prof.created_at)))
            f.addRow("Profile last updated", QLabel(f"{fmt_ns(self.prof.updated_at)} (version {self.prof.version_no})"))
            f.addRow(info_box("These are the dates of the <b>profile</b>. Generated outputs get their own "
                              "timestamps from the Timestamps tab — never these.", "info"))
        return w

    def _refresh_mode_labels(self):
        ops = self.prof.operations
        self.ops_label.setText(f"{len(ops)} saved edit(s): " + "; ".join(describe(o) for o in ops[:4]) +
                               ("…" if len(ops) > 4 else "") if ops else
                               "No edits yet — open this profile in the Editor to add them.")
        if self.prof.master_file_id:
            r = self.repo.file_row(self.prof.master_file_id)
            self.master_label.setText(f"Master: {r['original_name']} ({r['size'] // 1024} KB, SHA-256 "
                                      f"{r['sha256'][:12]}…)")
        else:
            self.master_label.setText("No master imported.")

    def _import_master(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Import edited master PDF", "", "PDF files (*.pdf)")
        if not fn:
            return
        info = inspect_pdf(fn)
        if not info.ok or info.needs_password:
            QMessageBox.warning(self, "Cannot use this PDF", info.error or "The PDF is password protected.")
            return
        self.prof.master_file_id = self.repo.store_file(fn, "master")
        self.mode_master.setChecked(True)
        self._refresh_mode_labels()
        if info.warnings:
            QMessageBox.information(self, "Master imported", "Imported. Notes:\n• " + "\n• ".join(info.warnings))

    def _matching(self):
        w = QWidget()
        f = QFormLayout(w)
        r = self.prof.rules
        f.addRow(info_box("A download is processed when its <b>name or hash</b> matches (and its website, if set). "
                          "A matching name alone does not guarantee the same document, so the content check pauses "
                          "processing for your decision when the new file looks substantially different.", "info"))
        self.filenames = QPlainTextEdit("\n".join(r.get("filenames", [])))
        self.filenames.setPlaceholderText("Report.pdf   (one per line, case-insensitive)")
        self.filenames.setFixedHeight(60)
        f.addRow("Exact filenames", self.filenames)
        self.patterns = QPlainTextEdit("\n".join(r.get("patterns", [])))
        self.patterns.setPlaceholderText("Report-*.pdf")
        self.patterns.setFixedHeight(50)
        f.addRow("Filename patterns", self.patterns)
        self.collision = QCheckBox("Also match Chrome's duplicate names, e.g. 'Report (1).pdf'")
        self.collision.setChecked(r.get("match_collision_names", True))
        f.addRow("", self.collision)
        self.hashes = QPlainTextEdit("\n".join(r.get("hashes", [])))
        self.hashes.setPlaceholderText("SHA-256 of exact original files (optional)")
        self.hashes.setFixedHeight(50)
        f.addRow("File hashes", self.hashes)
        hb = QPushButton("Add hash of a file…")
        hb.clicked.connect(self._add_hash)
        f.addRow("", hb)
        self.hosts = QLineEdit(", ".join(r.get("hosts", [])))
        self.hosts.setPlaceholderText("portal.example.com, *.example.org  (optional)")
        f.addRow("Websites", self.hosts)
        self.urls = QLineEdit(", ".join(r.get("url_patterns", [])))
        self.urls.setPlaceholderText("https://portal.example.com/reports/*  (optional)")
        f.addRow("URL patterns", self.urls)
        f.addRow(QLabel("<small>Website rules use the source URL Chrome records on the downloaded file "
                        "(Zone.Identifier) or the optional extension. No cookies or passwords are read.</small>"))
        self.content_check = QCheckBox("Pause for review if the content differs from the reference document")
        self.content_check.setChecked(r.get("content_check", True))
        f.addRow("", self.content_check)
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(0.05, 1.0)
        self.threshold.setSingleStep(0.05)
        self.threshold.setValue(float(r.get("similarity_threshold", 0.55)))
        f.addRow("Minimum similarity", self.threshold)
        row = QHBoxLayout()
        self.ref_label = QLabel("Reference: " + ("set" if self.prof.reference_fingerprint else "not set"))
        rb = QPushButton("Set reference from a PDF…")
        rb.clicked.connect(lambda: self._set_reference())
        row.addWidget(self.ref_label, 1)
        row.addWidget(rb)
        f.addRow(row)
        return w

    def _add_hash(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Choose file", "", "PDF files (*.pdf)")
        if fn:
            from ..fsutil import sha256_file
            self.hashes.appendPlainText(sha256_file(fn))

    def _set_reference(self, path=None):
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Reference PDF", "", "PDF files (*.pdf)")
        if path:
            try:
                self.prof.reference_fingerprint = fingerprint_file(path)
                self.ref_label.setText(f"Reference: {Path(path).name} ({self.prof.reference_fingerprint['pages']} "
                                       f"pages; only hashes are stored)")
            except Exception:
                QMessageBox.warning(self, "Reference", "That file could not be read as a PDF.")

    def _output(self):
        w = QWidget()
        f = QFormLayout(w)
        o = self.prof.output
        self.out_mode = QComboBox()
        for k, lbl in (("replace", "Replace the downloaded file in place (original is backed up first)"),
                       ("directory", "Save to a separate folder"),
                       ("profile_dir", "Save with the original name in a profile-specific folder")):
            self.out_mode.addItem(lbl, k)
        self.out_mode.setCurrentIndex(max(0, self.out_mode.findData(o.get("mode", "replace"))))
        f.addRow("Where", self.out_mode)
        row = QHBoxLayout()
        self.out_dir = QLineEdit(o.get("directory", ""))
        b = QPushButton("Browse…")
        b.clicked.connect(lambda: self.out_dir.setText(QFileDialog.getExistingDirectory(self, "Output folder")
                                                       or self.out_dir.text()))
        row.addWidget(self.out_dir)
        row.addWidget(b)
        f.addRow("Folder", row)
        self.template = QLineEdit(o.get("filename_template", "{name}"))
        self.template.setToolTip("{name} original filename, {stem} without .pdf, {profile}, {date}")
        f.addRow("File name", self.template)
        self.collision_policy = QComboBox()
        self.collision_policy.addItem("Replace Chrome's duplicate ('Report (1).pdf') in place", "in_place")
        self.collision_policy.addItem("Write to the canonical name ('Report.pdf') when that is our previous output",
                                      "canonical")
        self.collision_policy.setCurrentIndex(max(0, self.collision_policy.findData(
            o.get("collision_policy", "in_place"))))
        f.addRow("Duplicate names", self.collision_policy)
        self.open_after = QCheckBox("Open the result in my default PDF viewer")
        self.open_after.setChecked(o.get("open_after", False))
        f.addRow("", self.open_after)
        self.open_chrome = QCheckBox("Open the result in Chrome (new tab, local file)")
        self.open_chrome.setChecked(o.get("open_in_chrome", False))
        f.addRow("", self.open_chrome)
        f.addRow(info_box("A website tab that already shows the PDF is <b>not</b> redirected — the local result "
                          "opens as a new file. Unrelated files are never overwritten; a numbered name is used.",
                          "info"))
        return w

    def _timestamps(self):
        w = QWidget()
        v = QVBoxLayout(w)
        pol = TimestampPolicy.from_dict(self.prof.timestamp_policy)
        self.ts_group = QButtonGroup(self)
        self.ts_radios = {}
        for kind in ("download", "processing", "preserve", "custom"):
            rb = QRadioButton(TimestampPolicy(kind=kind).describe())
            self.ts_group.addButton(rb)
            self.ts_radios[kind] = rb
            v.addWidget(rb)
        self.ts_radios[pol.kind].setChecked(True)
        box = QGroupBox("Custom policy (D)")
        f = QFormLayout(box)
        self.c_src, self.c_fixed = self._source_row(pol.created, pol.created_fixed)
        self.m_src, self.m_fixed = self._source_row(pol.modified, pol.modified_fixed)
        f.addRow("Created", self._pair(self.c_src, self.c_fixed))
        f.addRow("Modified", self._pair(self.m_src, self.m_fixed))
        v.addWidget(box)
        v.addWidget(info_box(
            "<b>Three different kinds of dates:</b><br>"
            "1. <b>Filesystem timestamps</b> (set here) — what Explorer shows. Option A uses the time of the "
            "<i>new download</i>, never the date your master or profile was saved.<br>"
            "2. <b>Internal PDF metadata dates</b> (CreationDate/ModDate) — preserved unchanged unless you add the "
            "explicit, authorised 'Internal PDF dates' edit.<br>"
            "3. <b>Dates printed on the page</b> — only change through an explicit text edit you confirm.<br>"
            "The system clock is never changed. Actual values are recorded and verified for every output.", "info"))
        v.addStretch(1)
        return w

    def _source_row(self, src, fixed):
        cb = QComboBox()
        for s in SOURCES:
            cb.addItem(SOURCE_LABELS[s], s)
        cb.setCurrentIndex(max(0, cb.findData(src)))
        dt = QDateTimeEdit()
        dt.setCalendarPopup(True)
        dt.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        if fixed:
            dt.setDateTime(QDateTime.fromMSecsSinceEpoch(iso_to_ns(fixed) // 1_000_000))
        else:
            dt.setDateTime(QDateTime.currentDateTime())
        dt.setEnabled(src == "fixed")
        cb.currentIndexChanged.connect(lambda _i: dt.setEnabled(cb.currentData() == "fixed"))
        return cb, dt

    @staticmethod
    def _pair(a, b):
        w = QWidget()
        l = QHBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.addWidget(a)
        l.addWidget(b)
        return w

    def _options(self):
        w = QWidget()
        f = QFormLayout(w)
        o = self.prof.options
        self.dry = QCheckBox("Dry run — generate and validate, but never publish")
        self.dry.setChecked(o.get("dry_run", False))
        f.addRow(self.dry)
        self.strict = QCheckBox("Strict verification — do not publish if an edit cannot verify its own result")
        self.strict.setChecked(o.get("strict_verification", True))
        f.addRow(self.strict)
        self.allow_signed = QCheckBox("Allow editing digitally signed PDFs (this INVALIDATES their signature)")
        self.allow_signed.setChecked(o.get("allow_signed", False))
        f.addRow(self.allow_signed)
        self.allow_enc = QCheckBox("Allow editing encrypted PDFs that open without a password (protection is "
                                   "removed in the output)")
        self.allow_enc.setChecked(o.get("allow_encrypted", False))
        f.addRow(self.allow_enc)
        self.notify = QCheckBox("Show a notification when a download is processed")
        self.notify.setChecked(o.get("notify", True))
        f.addRow(self.notify)
        f.addRow(info_box("Password-protected PDFs are never processed, and this application never asks for or "
                          "stores document or website passwords.", "info"))
        return w

    def _versions(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.ver_list = QListWidget()
        for r in self.repo.versions(self.prof.id):
            it = QListWidgetItem(f"v{r['version_no']} — {fmt_ns(r['created_at'])} — {r['mode']}, "
                                 f"{r['op_count']} edit(s) — {r['note'] or ''}")
            it.setData(Qt.UserRole, r["id"])
            self.ver_list.addItem(it)
        v.addWidget(self.ver_list)
        b = QPushButton("Restore selected version (creates a new version)")
        b.clicked.connect(self._restore_version)
        v.addWidget(b)
        return w

    def _restore_version(self):
        it = self.ver_list.currentItem()
        if not it:
            return
        if confirm(self, "Restore version", "Restore this version? Your current settings remain available as an "
                                            "older version."):
            self.repo.restore_version(self.prof.id, it.data(Qt.UserRole))
            self.prof = self.repo.get_profile(self.prof.id)
            QMessageBox.information(self, "Restored", "Version restored.")
            self.accept()

    # ------------------------------------------------------------------ save
    def collect(self) -> Profile:
        p = self.prof
        p.name = self.name.text().strip()
        p.enabled = self.enabled.isChecked()
        p.priority = self.priority.value()
        p.mode = "master" if self.mode_master.isChecked() else "operations"
        split = lambda s: [x.strip() for x in s.split(",") if x.strip()]  # noqa: E731
        p.rules.update({"filenames": _lines(self.filenames), "patterns": _lines(self.patterns),
                        "hashes": _lines(self.hashes), "hosts": split(self.hosts.text()),
                        "url_patterns": split(self.urls.text()), "match_collision_names": self.collision.isChecked(),
                        "content_check": self.content_check.isChecked(),
                        "similarity_threshold": round(self.threshold.value(), 2)})
        p.output.update({"mode": self.out_mode.currentData(), "directory": self.out_dir.text().strip(),
                         "filename_template": self.template.text().strip() or "{name}",
                         "collision_policy": self.collision_policy.currentData(),
                         "open_after": self.open_after.isChecked(), "open_in_chrome": self.open_chrome.isChecked()})
        kind = next(k for k, rb in self.ts_radios.items() if rb.isChecked())
        iso = lambda dt: dt.dateTime().toPython().astimezone().isoformat()  # noqa: E731
        p.timestamp_policy = {"kind": kind, "created": self.c_src.currentData(), "modified": self.m_src.currentData(),
                              "created_fixed": iso(self.c_fixed) if self.c_src.currentData() == "fixed" else None,
                              "modified_fixed": iso(self.m_fixed) if self.m_src.currentData() == "fixed" else None}
        p.options.update({"dry_run": self.dry.isChecked(), "strict_verification": self.strict.isChecked(),
                          "allow_signed": self.allow_signed.isChecked(), "allow_encrypted": self.allow_enc.isChecked(),
                          "notify": self.notify.isChecked()})
        return p

    def _save(self):
        p = self.collect()
        errs = p.validate()
        if p.enabled and errs:
            QMessageBox.warning(self, "Profile incomplete", "Fix these before enabling automatic processing:\n• " +
                                "\n• ".join(errs))
            return
        if p.enabled and not self.was_enabled:
            folders = ", ".join(self.service.settings()["watch_folders"])
            what = {"replace": "REPLACE the downloaded file (the original is backed up first)",
                    "directory": f"write the result to {p.output.get('directory')}",
                    "profile_dir": "write the result to the profile's output folder"}[p.output["mode"]]
            if not confirm(self, "Enable automatic processing?",
                           f"From now on, when a PDF matching '{p.name}' appears in:\n{folders}\n\nthe application will "
                           f"{'use your saved master' if p.mode == 'master' else 'apply your saved edits'} and {what}."
                           f"\n\nTimestamp policy: {TimestampPolicy.from_dict(p.timestamp_policy).describe()}"
                           + ("\n\nDRY RUN is on: nothing will be published." if p.options.get("dry_run") else ""),
                           ok_text="Enable"):
                return
        try:
            self.prof = self.repo.save_profile(p, note="Edited in profile dialog", validate=p.enabled)
        except PRMError as e:
            QMessageBox.warning(self, "Cannot save", e.user_message)
            return
        self.accept()
