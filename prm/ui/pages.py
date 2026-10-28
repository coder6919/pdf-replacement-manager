"""Dashboard, profiles, queue/history, settings (incl. backup) and diagnostics screens."""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton, QSpinBox,
                               QSplitter, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget)

from .. import __version__
from ..backup import export_backup, restore_backup, verify_backup
from ..errors import PRMError
from ..fsutil import find_chrome, open_with_default_app
from ..operations import describe
from ..timestamps import ns_to_iso
from .common import (STATE_COLORS, STATE_LABELS, card, confirm, error, fmt_ns, fmt_short, heading, info_box,
                     reveal_in_explorer)
from .profile_dialog import ProfileDialog


def _table(headers) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setStretchLastSection(True)
    t.setAlternatingRowColors(True)
    return t


def _cell(text, data=None, color=None) -> QTableWidgetItem:
    it = QTableWidgetItem(str(text))
    if data is not None:
        it.setData(Qt.UserRole, data)
    if color:
        from PySide6.QtGui import QColor
        it.setForeground(QColor(color))
    return it


# =========================================================================== dashboard
class DashboardPage(QWidget):
    navigate = Signal(str)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        v = QVBoxLayout(self)
        v.addWidget(heading("Dashboard", "Automatic, persistent edits for PDFs you download."))
        grid = QGridLayout()
        self.mon_card, ml = card("Download monitoring")
        self.mon_label = QLabel()
        self.mon_label.setWordWrap(True)
        self.mon_btn = QPushButton()
        self.mon_btn.clicked.connect(self.toggle_monitoring)
        ml.addWidget(self.mon_label)
        ml.addWidget(self.mon_btn)
        grid.addWidget(self.mon_card, 0, 0)
        pc, pl = card("Profiles")
        self.prof_label = QLabel()
        b = QPushButton("Manage profiles")
        b.clicked.connect(lambda: self.navigate.emit("profiles"))
        pl.addWidget(self.prof_label)
        pl.addWidget(b)
        grid.addWidget(pc, 0, 1)
        jc, jl = card("Today")
        self.today_label = QLabel()
        b2 = QPushButton("Queue && history")
        b2.clicked.connect(lambda: self.navigate.emit("jobs"))
        jl.addWidget(self.today_label)
        jl.addWidget(b2)
        grid.addWidget(jc, 0, 2)
        v.addLayout(grid)
        row = QHBoxLayout()
        b3 = QPushButton("Process a PDF now (one time)…")
        b3.clicked.connect(self.manual)
        b4 = QPushButton("Open the editor")
        b4.clicked.connect(lambda: self.navigate.emit("editor"))
        row.addWidget(b3)
        row.addWidget(b4)
        row.addStretch(1)
        v.addLayout(row)
        self.attention = QGroupBox("Needs your attention")
        al = QVBoxLayout(self.attention)
        self.att_table = _table(["When", "File", "Status", "Reason"])
        self.att_table.doubleClicked.connect(lambda: self.navigate.emit("jobs"))
        al.addWidget(self.att_table)
        v.addWidget(self.attention, 1)
        rg = QGroupBox("Recent activity")
        rl = QVBoxLayout(rg)
        self.recent = _table(["When", "File", "Profile", "Result"])
        rl.addWidget(self.recent)
        v.addWidget(rg, 1)

    def refresh(self):
        s = self.service.settings()
        on = self.service.monitoring
        folders = "<br>".join(s["watch_folders"]) or "(none)"
        self.mon_label.setText(("<b style='color:#1e7e34'>● Running</b>" if on else
                                "<b style='color:#c62828'>● Paused</b>") + f"<br>Watching:<br>{folders}"
                               + ("<br><b>Global dry run is ON</b>" if s["global_dry_run"] else ""))
        self.mon_btn.setText("Pause monitoring" if on else "Start monitoring")
        profs = self.service.repo.list_profiles()
        en = [p for p in profs if p.enabled]
        self.prof_label.setText(f"{len(profs)} profile(s), {len(en)} enabled<br>" +
                                "<br>".join(f"• {p.name} ({p.mode})" for p in en[:5]))
        start = int(datetime.now().replace(hour=0, minute=0, second=0).timestamp() * 1e9)
        rows = self.service.repo.db.q("SELECT state, COUNT(*) FROM jobs WHERE created_at>=? GROUP BY state", (start,))
        counts = {r[0]: r[1] for r in rows}
        self.today_label.setText("<br>".join(f"{STATE_LABELS.get(k, k)}: {v}" for k, v in counts.items())
                                 or "No downloads processed today.")
        att = self.service.repo.jobs(("needs_review", "failed"), limit=20)
        self._fill(self.att_table, [(fmt_short(j["created_at"]), j["file_name"],
                                     (STATE_LABELS.get(j["state"]), STATE_COLORS.get(j["state"])), j["error"] or "")
                                    for j in att])
        self.attention.setVisible(bool(att))
        names = {p.id: p.name for p in self.service.repo.list_profiles(include_deleted=True)}
        rec = self.service.repo.jobs(limit=15)
        self._fill(self.recent, [(fmt_short(j["created_at"]), j["file_name"], names.get(j["profile_id"], "—"),
                                  (STATE_LABELS.get(j["state"], j["state"]), STATE_COLORS.get(j["state"])))
                                 for j in rec])

    @staticmethod
    def _fill(t, rows):
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for c, val in enumerate(r):
                t.setItem(i, c, _cell(val[0], color=val[1]) if isinstance(val, tuple) else _cell(val))
        t.resizeColumnsToContents()
        t.horizontalHeader().setStretchLastSection(True)

    def toggle_monitoring(self):
        if self.service.monitoring:
            self.service.watcher.stop()
            self.service.set_settings(monitoring_enabled=False)
        else:
            self.service.set_settings(monitoring_enabled=True)
            self.service.start_monitoring()
        self.refresh()

    def manual(self):
        manual_process(self, self.service)
        self.refresh()


def manual_process(parent, service, profile_id=None):
    fn, _ = QFileDialog.getOpenFileName(parent, "Choose a PDF to process once", "", "PDF files (*.pdf)")
    if not fn:
        return
    profs = service.repo.list_profiles()
    if not profs:
        error(parent, "Create a profile first.")
        return
    if profile_id is None:
        names = [p.name for p in profs]
        name, ok = QInputDialog.getItem(parent, "Profile", "Apply which profile?", names, 0, False)
        if not ok:
            return
        profile_id = profs[names.index(name)].id
    dry = confirm(parent, "Dry run?", "Do a dry run first (generate and validate only, nothing is replaced)?\n\n"
                                      "Choose Cancel to process for real.", ok_text="Dry run")
    jid = service.submit_file(fn, profile_id=profile_id, dry_run=dry)
    j = service.repo.job(jid)
    if j["state"] in ("done", "dry_run"):
        msg = (f"Dry run OK. Preview: {j['staging_path']}\nWould publish to: {j['output_path']}" if j["state"] ==
               "dry_run" else f"Done. Output: {j['output_path']}")
        if QMessageBox.question(parent, "Result", msg + "\n\nOpen the result?") == QMessageBox.Yes:
            open_with_default_app(j["staging_path"] if j["state"] == "dry_run" else j["output_path"])
    else:
        error(parent, f"{STATE_LABELS.get(j['state'], j['state'])}: {j['error']}")


# =========================================================================== profiles
class ProfilesPage(QWidget):
    openInEditor = Signal(int)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        v = QVBoxLayout(self)
        v.addWidget(heading("Profiles", "Each profile remembers how to modify one kind of downloaded PDF. "
                                        "Profiles are saved permanently and versioned."))
        row = QHBoxLayout()
        for label, fn in (("New profile…", self.new), ("Edit…", self.edit), ("Open in editor", self.open_editor),
                          ("Enable / disable", self.toggle), ("Process a PDF now…", self.process_now),
                          ("Delete…", self.delete)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self.table = _table(["Name", "Status", "Mode", "Matches", "Edits / master", "Timestamps", "Version",
                             "Updated"])
        self.table.doubleClicked.connect(self.edit)
        v.addWidget(self.table, 1)

    def refresh(self):
        profs = self.service.repo.list_profiles()
        self.table.setRowCount(len(profs))
        for i, p in enumerate(profs):
            r = p.rules
            match = ", ".join(r.get("filenames", []) + r.get("patterns", [])) or f"{len(r.get('hashes', []))} hash(es)"
            if r.get("hosts"):
                match += " @ " + ", ".join(r["hosts"])
            what = (f"{len(p.operations)} edit(s)" if p.mode == "operations" else
                    ("master saved" if p.master_file_id else "NO MASTER"))
            vals = [p.name, ("Enabled", "#1e7e34") if p.enabled else ("Disabled", "#757575"),
                    "Apply edits" if p.mode == "operations" else "Saved master", match, what,
                    p.timestamp_policy.get("kind", "download"), f"v{p.version_no}", fmt_short(p.updated_at)]
            for c, val in enumerate(vals):
                it = _cell(val[0], color=val[1]) if isinstance(val, tuple) else _cell(val)
                it.setData(Qt.UserRole, p.id)
                self.table.setItem(i, c, it)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)

    def _current(self):
        it = self.table.item(self.table.currentRow(), 0) if self.table.currentRow() >= 0 else None
        return self.service.repo.get_profile(it.data(Qt.UserRole)) if it else None

    def new(self):
        if ProfileDialog(self.service, None, self).exec():
            self.refresh()

    def edit(self):
        p = self._current()
        if p and ProfileDialog(self.service, p, self).exec():
            self.refresh()

    def open_editor(self):
        p = self._current()
        if p:
            self.openInEditor.emit(p.id)

    def toggle(self):
        p = self._current()
        if not p:
            return
        if not p.enabled:
            errs = p.validate()
            if errs:
                error(self, "Cannot enable yet:\n• " + "\n• ".join(errs))
                return
            if not confirm(self, "Enable automatic processing", f"Matching downloads will be processed with "
                                                                f"'{p.name}' automatically. Continue?", "Enable"):
                return
        self.service.repo.set_enabled(p.id, not p.enabled)
        self.refresh()

    def process_now(self):
        p = self._current()
        manual_process(self, self.service, p.id if p else None)

    def delete(self):
        p = self._current()
        if p and confirm(self, "Delete profile", f"Delete '{p.name}'? Its history, versions and saved master stay "
                                                 "in the database and in backups.", "Delete", danger=True):
            self.service.repo.delete_profile(p.id)
            self.refresh()


# =========================================================================== jobs
FILTERS = {"All": None, "Active": ("queued", "processing"), "Needs review": ("needs_review",),
           "Failed": ("failed",), "Done": ("done",), "Dry runs": ("dry_run",),
           "Skipped / ignored": ("skipped", "ignored", "superseded")}


class JobsPage(QWidget):
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        v = QVBoxLayout(self)
        v.addWidget(heading("Queue & history", "Every detected download, what was done, and why."))
        row = QHBoxLayout()
        self.filter = QComboBox()
        self.filter.addItems(list(FILTERS))
        self.filter.currentIndexChanged.connect(self.refresh)
        row.addWidget(QLabel("Show"))
        row.addWidget(self.filter)
        row.addStretch(1)
        self.btns = {}
        for label, fn in (("Retry", self.retry), ("Approve…", self.approve), ("Restore original…", self.restore),
                          ("Open output", self.open_output), ("Show in folder", self.show_folder)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
            self.btns[label] = b
        v.addLayout(row)
        sp = QSplitter(Qt.Vertical)
        self.table = _table(["#", "Detected", "File", "Profile", "Status", "Duration", "Output"])
        self.table.itemSelectionChanged.connect(self.show_details)
        sp.addWidget(self.table)
        self.details = QTextBrowser()
        self.details.setOpenExternalLinks(False)
        sp.addWidget(self.details)
        sp.setSizes([300, 300])
        v.addWidget(sp, 1)
        self._sel = None

    def refresh(self):
        rows = self.service.repo.jobs(FILTERS[self.filter.currentText()], limit=500)
        names = {p.id: p.name for p in self.service.repo.list_profiles(include_deleted=True)}
        sel = self._sel
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for i, j in enumerate(rows):
            dur = (f"{(j['finished_ns'] - j['started_ns']) / 1e9:.1f}s" if j["finished_ns"] and j["started_ns"]
                   else "")
            vals = [j["id"], fmt_short(j["detected_ns"]), j["file_name"], names.get(j["profile_id"], "—"),
                    (STATE_LABELS.get(j["state"], j["state"]), STATE_COLORS.get(j["state"])), dur,
                    j["output_path"] or ""]
            for c, val in enumerate(vals):
                it = _cell(val[0], color=val[1]) if isinstance(val, tuple) else _cell(val)
                it.setData(Qt.UserRole, j["id"])
                self.table.setItem(i, c, it)
            if j["id"] == sel:
                self.table.selectRow(i)
        self.table.blockSignals(False)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.show_details()

    def _job(self):
        r = self.table.currentRow()
        if r < 0 or not self.table.item(r, 0):
            return None
        return self.service.repo.job(self.table.item(r, 0).data(Qt.UserRole))

    def show_details(self):
        j = self._job()
        self._sel = j["id"] if j else None
        for b in self.btns.values():
            b.setEnabled(bool(j))
        if not j:
            self.details.setHtml("<i>Select a job to see its details.</i>")
            return
        self.btns["Retry"].setEnabled(j["state"] in ("failed", "needs_review", "dry_run"))
        self.btns["Approve…"].setEnabled(j["state"] == "needs_review")
        self.btns["Restore original…"].setEnabled(bool(j["backup_file_id"]))
        esc = lambda s: (str(s) if s is not None else "—").replace("&", "&amp;").replace("<", "&lt;")  # noqa: E731
        h = [f"<h3>Job #{j['id']} — {esc(j['file_name'])}</h3>",
             f"<p><b style='color:{STATE_COLORS.get(j['state'], '#000')}'>{STATE_LABELS.get(j['state'], j['state'])}"
             f"</b> {esc(j['error']) if j['error'] else ''}</p>",
             "<table cellspacing=3>",
             f"<tr><td>Source path</td><td>{esc(j['source_path'])}</td></tr>",
             f"<tr><td>Source website</td><td>{esc(j['source_url'])}</td></tr>",
             f"<tr><td>Match</td><td>{esc(j['match_reason'])}</td></tr>",
             f"<tr><td>Mode</td><td>{esc(j['mode'])}</td></tr>",
             f"<tr><td>Output</td><td>{esc(j['output_path'])}</td></tr>",
             f"<tr><td>SHA-256 in / out</td><td>{esc((j['sha256'] or '')[:16])}… / "
             f"{esc((j['output_sha256'] or '')[:16])}…</td></tr>",
             f"<tr><td>Download created</td><td>{fmt_ns(j['download_created_ns'])}</td></tr>",
             f"<tr><td>Download modified</td><td>{fmt_ns(j['download_modified_ns'])}</td></tr>",
             f"<tr><td>Detected</td><td>{fmt_ns(j['detected_ns'])}</td></tr>",
             f"<tr><td>Attempts</td><td>{j['attempts']}</td></tr></table>"]
        if j["timestamps"]:
            ts = json.loads(j["timestamps"])
            h.append("<h4>Output timestamps</h4><table cellspacing=3>"
                     f"<tr><td>Policy</td><td>{esc(ts.get('policy_description'))}</td></tr>"
                     f"<tr><td>Created (applied)</td><td>{fmt_ns(ts['actual']['created_ns'])}</td></tr>"
                     f"<tr><td>Modified (applied)</td><td>{fmt_ns(ts['actual']['modified_ns'])}</td></tr>"
                     f"<tr><td>UTC</td><td>{ts['actual']['created']} / {ts['actual']['modified']}</td></tr>"
                     f"<tr><td>Verified</td><td>{'yes' if ts['verified'] else 'NO'} "
                     f"({esc(ts.get('filesystem'))}, tolerance {ts.get('tolerance_ns', 0) // 100} ticks)</td></tr>"
                     "</table>")
        if j["validation"]:
            val = json.loads(j["validation"])
            if "considered" in val:
                h.append("<h4>Profiles considered</h4>" + "".join(f"• {esc(n)}: {esc(r)}<br>"
                                                                  for n, r in val["considered"]))
            if "checks" in val:
                h.append(f"<h4>Validation</h4>{'OK' if val['ok'] else 'FAILED: ' + esc('; '.join(val['problems']))}"
                         f" — {esc(val['checks'])}")
        if j["op_results"]:
            h.append("<h4>Edits</h4>")
            for r in json.loads(j["op_results"]):
                h.append(f"{'✓' if r['ok'] else '✗'} {esc(r['type'])} ({esc(r['effect'])}) ×{r['count']}"
                         f"{' — ' + esc(r['error']) if r['error'] else ''}"
                         f"{' — ' + esc('; '.join(r['warnings'])) if r['warnings'] else ''}<br>")
        h.append("<h4>Events</h4>")
        for e in self.service.repo.events(j["id"]):
            h.append(f"{fmt_short(e['ts'])} [{e['level']}] {esc(e['message'])}<br>")
        self.details.setHtml("".join(h))

    def retry(self):
        j = self._job()
        if j:
            self.service.processor.retry(j["id"])
            self.service.repo.update_job(j["id"], dry_run=0)
            if self.service.watcher:
                self.service.watcher.wake()
            else:
                self.service.processor.process(j["id"])
            self.refresh()

    def approve(self):
        j = self._job()
        if not j:
            return
        profs = self.service.repo.list_profiles()
        if not profs:
            return
        names = [p.name for p in profs]
        cur = next((i for i, p in enumerate(profs) if p.id == j["profile_id"]), 0)
        name, ok = QInputDialog.getItem(self, "Approve", f"{j['error']}\n\nProcess with profile:", names, cur, False)
        if not ok:
            return
        upd = confirm(self, "Reference", "Also use this download as the new reference document for future "
                                         "content checks?", "Yes, update reference")
        self.service.processor.approve(j["id"], profs[names.index(name)].id, update_reference=upd)
        if self.service.watcher:
            self.service.watcher.wake()
        self.refresh()

    def restore(self):
        j = self._job()
        if not j:
            return
        choice = QMessageBox.question(self, "Restore original",
                                      f"Restore the original download to\n{j['source_path']}?\n\n"
                                      "Yes = original location (an unrelated file there is never overwritten)\n"
                                      "No = choose another location", QMessageBox.Yes | QMessageBox.No |
                                      QMessageBox.Cancel)
        if choice == QMessageBox.Cancel:
            return
        dest = None
        if choice == QMessageBox.No:
            fn, _ = QFileDialog.getSaveFileName(self, "Restore to", j["file_name"], "PDF files (*.pdf)")
            if not fn:
                return
            dest = Path(fn)
        try:
            out = self.service.processor.restore_original(j["id"], dest)
            QMessageBox.information(self, "Restored", f"Original restored to:\n{out}")
        except PRMError as e:
            error(self, e.user_message)
        self.refresh()

    def open_output(self):
        j = self._job()
        p = j and (j["output_path"] if j["state"] == "done" else j["staging_path"])
        if p and Path(p).exists():
            open_with_default_app(p)

    def show_folder(self):
        j = self._job()
        if j:
            reveal_in_explorer(j["output_path"] or j["source_path"])


# =========================================================================== settings
class SettingsPage(QWidget):
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        v = QVBoxLayout(self)
        v.addWidget(heading("Settings", f"Data folder: {service.paths.root}"))
        s = service.settings()
        g = QGroupBox("Download monitoring")
        f = QFormLayout(g)
        self.folders = QListWidget()
        self.folders.addItems(s["watch_folders"])
        self.folders.setFixedHeight(70)
        fb = QHBoxLayout()
        add = QPushButton("Add folder…")
        add.clicked.connect(self._add_folder)
        rem = QPushButton("Remove")
        rem.clicked.connect(lambda: self.folders.takeItem(self.folders.currentRow()))
        fb.addWidget(add)
        fb.addWidget(rem)
        fb.addStretch(1)
        f.addRow("Watched folders", self.folders)
        f.addRow("", fb)
        self.stable = QDoubleSpinBox()
        self.stable.setRange(0.5, 60)
        self.stable.setValue(float(s["stable_seconds"]))
        self.stable.setSuffix(" s")
        f.addRow("Wait until unchanged for", self.stable)
        self.global_dry = QCheckBox("Global dry run (never publish anything)")
        self.global_dry.setChecked(s["global_dry_run"])
        f.addRow(self.global_dry)
        v.addWidget(g)
        g2 = QGroupBox("Application")
        f2 = QFormLayout(g2)
        self.autostart = QCheckBox("Start with Windows (per-user, minimised to the tray)")
        self.autostart.setChecked(s["start_with_windows"])
        f2.addRow(self.autostart)
        self.tray = QCheckBox("Closing the window keeps monitoring in the system tray")
        self.tray.setChecked(s["minimize_to_tray"])
        f2.addRow(self.tray)
        self.notif = QCheckBox("Show notifications")
        self.notif.setChecked(s["notifications"])
        f2.addRow(self.notif)
        self.retention = QSpinBox()
        self.retention.setRange(1, 3650)
        self.retention.setValue(int(s["original_retention_days"]))
        self.retention.setSuffix(" days")
        f2.addRow("Keep original backups for", self.retention)
        v.addWidget(g2)
        g3 = QGroupBox("Optional Chrome extension bridge")
        f3 = QFormLayout(g3)
        self.bridge = QCheckBox("Enable local bridge (127.0.0.1 only, token protected)")
        self.bridge.setChecked(s["bridge_enabled"])
        f3.addRow(self.bridge)
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(int(s["bridge_port"]))
        f3.addRow("Port", self.port)
        tok = QHBoxLayout()
        self.token = QLineEdit(service.db.get_setting("bridge_token"))
        self.token.setReadOnly(True)
        self.token.setEchoMode(QLineEdit.Password)
        cp = QPushButton("Copy")
        cp.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.token.text()))
        rg = QPushButton("New token")
        rg.clicked.connect(self._new_token)
        tok.addWidget(self.token)
        tok.addWidget(cp)
        tok.addWidget(rg)
        f3.addRow("Pairing token", tok)
        f3.addRow(QLabel("<small>Not required: Chrome downloads are detected without any extension. The extension "
                         "only adds website hints for blob/data downloads and a status popup.</small>"))
        v.addWidget(g3)
        save = QPushButton("Save settings")
        save.clicked.connect(self.save)
        v.addWidget(save, 0, Qt.AlignLeft)
        g4 = QGroupBox("Backup & restore")
        b = QHBoxLayout(g4)
        for label, fn in (("Create backup…", self.backup), ("Restore backup…", self.restore),
                          ("Verify stored files", self.verify)):
            x = QPushButton(label)
            x.clicked.connect(fn)
            b.addWidget(x)
        b.addStretch(1)
        v.addWidget(g4)
        v.addStretch(1)

    def refresh(self):
        pass

    def _add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Folder to watch")
        if d:
            self.folders.addItem(d)

    def _new_token(self):
        import secrets
        t = secrets.token_urlsafe(24)
        self.service.db.set_setting("bridge_token", t)
        self.token.setText(t)

    def save(self):
        folders = [self.folders.item(i).text() for i in range(self.folders.count())]
        missing = [f for f in folders if not Path(f).is_dir()]
        if missing:
            error(self, "These folders do not exist:\n" + "\n".join(missing))
            return
        try:
            self.service.set_settings(watch_folders=folders, stable_seconds=self.stable.value(),
                                      global_dry_run=self.global_dry.isChecked(),
                                      start_with_windows=self.autostart.isChecked(),
                                      minimize_to_tray=self.tray.isChecked(), notifications=self.notif.isChecked(),
                                      original_retention_days=self.retention.value(),
                                      bridge_enabled=self.bridge.isChecked(), bridge_port=self.port.value())
        except OSError as e:
            error(self, f"Could not change the Windows startup setting: {e}")
        self.service.start_monitoring() if self.service.settings()["monitoring_enabled"] else None
        if self.bridge.isChecked():
            try:
                self.service.start_bridge()
            except OSError as e:
                error(self, f"Bridge could not start on port {self.port.value()}: {e}")
        else:
            self.service.stop_bridge()
        QMessageBox.information(self, "Settings", "Settings saved.")

    def backup(self):
        default = str(Path.home() / f"PDFReplacementManager-backup-{datetime.now():%Y%m%d-%H%M}.zip")
        fn, _ = QFileDialog.getSaveFileName(self, "Create backup", default, "Backup (*.zip)")
        if not fn:
            return
        inc = confirm(self, "Backup", "Include backed-up original downloads? (They may contain sensitive "
                                      "documents and make the backup larger.)", "Include originals")
        try:
            m = export_backup(self.service, fn, include_originals=inc)
            QMessageBox.information(self, "Backup", f"Backup created and verified: {len(m['files'])} files.\n{fn}\n\n"
                                                    "The backup is not encrypted; store it somewhere safe.")
        except Exception as e:
            error(self, f"Backup failed: {e}")

    def restore(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Restore backup", str(Path.home()), "Backup (*.zip)")
        if not fn:
            return
        try:
            m = verify_backup(fn)
        except Exception as e:
            error(self, f"This backup cannot be used: {e}")
            return
        if not confirm(self, "Restore", f"Restore backup from {m['created']}?\n\nProfiles, versions and history are "
                                        "replaced by the backup's. Your current database is kept as a pre-restore "
                                        "copy; stored files are merged (nothing is deleted).", "Restore", danger=True):
            return
        try:
            r = restore_backup(self.service, fn)
            self.service.start()
            QMessageBox.information(self, "Restore", f"Restored. {r['files_restored']} file(s) restored.\n"
                                                     f"Previous database kept at:\n{r['pre_restore_copy']}")
        except Exception as e:
            error(self, f"Restore failed: {e}\nYour previous data was kept.")
            self.service.start()

    def verify(self):
        res = self.service.repo.verify_all_files()
        bad = [r for r in res if not r["ok"]]
        if bad:
            error(self, f"{len(bad)} of {len(res)} stored files have problems:\n" +
                  "\n".join(f"{r['kind']}: {r['path']} — {r['message']}" for r in bad[:15]))
        else:
            QMessageBox.information(self, "Verify", f"All {len(res)} stored files are present and intact.")


# =========================================================================== diagnostics
class DiagnosticsPage(QWidget):
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        v = QVBoxLayout(self)
        v.addWidget(heading("Logs & diagnostics", "Explains why a file was or was not processed. Logs never "
                                                  "contain document text, passwords or cookies."))
        row = QHBoxLayout()
        b = QPushButton("Why was / wasn't a file processed?…")
        b.clicked.connect(self.diagnose)
        row.addWidget(b)
        b2 = QPushButton("Open data folder")
        b2.clicked.connect(lambda: os.startfile(str(self.service.paths.root)))
        row.addWidget(b2)
        b3 = QPushButton("Refresh log")
        b3.clicked.connect(self.refresh)
        row.addWidget(b3)
        row.addStretch(1)
        v.addLayout(row)
        self.env = QLabel()
        self.env.setWordWrap(True)
        self.env.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.env)
        self.out = QTextBrowser()
        v.addWidget(self.out, 1)
        self.log = _table(["Time (UTC)", "Level", "Job", "Message"])
        v.addWidget(self.log, 2)

    def refresh(self):
        import pikepdf
        import pymupdf
        from PySide6 import __version__ as qtv
        tess = bool(os.environ.get("TESSDATA_PREFIX")) or bool(shutil.which("tesseract"))
        s = self.service.settings()
        w = self.service.watcher
        self.env.setText(
            f"<b>Version</b> {__version__} · Python {sys.version.split()[0]} · PyMuPDF {pymupdf.VersionBind} "
            f"(MuPDF {pymupdf.VersionFitz}) · pikepdf {pikepdf.__version__} · PySide6 {qtv}<br>"
            f"<b>Monitoring</b> {'running' if self.service.monitoring else 'paused'}; folders: "
            + ", ".join(f"{f} ({'ok' if Path(f).is_dir() else 'MISSING'})" for f in s["watch_folders"])
            + (f"; waiting for {len(w.pending_paths())} download(s) to finish" if w else "")
            + f"<br><b>Chrome</b> {'found' if find_chrome() else 'not found'} · <b>OCR (Tesseract)</b> "
              f"{'available' if tess else 'not installed'} · <b>Bridge</b> "
              f"{'on' if self.service.bridge else 'off'}")
        p = self.service.paths.logs / "prm.log.jsonl"
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-300:] if p.exists() else []
        self.log.setRowCount(len(lines))
        for i, ln in enumerate(reversed(lines)):
            try:
                d = json.loads(ln)
            except json.JSONDecodeError:
                d = {"msg": ln}
            for c, val in enumerate((d.get("ts", ""), d.get("level", ""), d.get("job_id", ""), d.get("msg", "")
                                     + (f" [{d['error_code']}]" if d.get("error_code") else ""))):
                self.log.setItem(i, c, _cell(val))
        self.log.resizeColumnsToContents()
        self.log.horizontalHeader().setStretchLastSection(True)

    def diagnose(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Choose a PDF", str(Path(self.service.settings()["watch_folders"][0])
                                                                       if self.service.settings()["watch_folders"]
                                                                       else Path.home()), "PDF files (*.pdf)")
        if not fn:
            return
        d = self.service.diagnose_file(fn)
        esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;")  # noqa: E731
        self.out.setHtml(f"<h3>{esc(fn)}</h3><table cellspacing=4>" + "".join(
            f"<tr><td><b>{esc(k)}</b></td><td>{esc(v)}</td></tr>" for k, v in d["steps"]) + "</table>")
