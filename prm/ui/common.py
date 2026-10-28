"""Shared UI helpers."""
from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap, QPen, QBrush
from PySide6.QtWidgets import (QColorDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from ..timestamps import ns_to_local_display

STATE_COLORS = {
    "done": "#1e7e34", "failed": "#c62828", "needs_review": "#b26a00", "queued": "#1565c0",
    "processing": "#1565c0", "skipped": "#757575", "ignored": "#757575", "dry_run": "#6a1b9a",
    "superseded": "#757575",
}
STATE_LABELS = {
    "done": "Done", "failed": "Failed", "needs_review": "Needs review", "queued": "Queued",
    "processing": "Processing", "skipped": "No matching profile", "ignored": "Own output (ignored)",
    "dry_run": "Dry run", "superseded": "Superseded",
}
EFFECT_TEXT = {
    "content-replace": ("Genuine edit", "#1e7e34",
                        "The original glyphs are removed from the page content and the new text is written in. "
                        "Nothing is hidden underneath."),
    "content-removal": ("Genuine removal", "#1e7e34",
                        "Content is permanently deleted from the page (not covered up)."),
    "overlay": ("Overlay", "#1565c0", "New content is drawn on top. The underlying page content is unchanged."),
    "annotation": ("Annotation", "#6a1b9a",
                   "A separate annotation object. The page content underneath is unchanged and the annotation can be "
                   "removed in other PDF editors."),
    "structure": ("Structure", "#455a64", "Changes pages, bookmarks or form fields."),
    "metadata": ("Metadata", "#455a64", "Changes the document information dictionary only."),
}


def fmt_ns(ns) -> str:
    return ns_to_local_display(ns) if ns else "—"


def fmt_short(ns) -> str:
    if not ns:
        return "—"
    dt = datetime.fromtimestamp(ns / 1e9)
    return dt.strftime("%H:%M:%S") if dt.date() == datetime.now().date() else dt.strftime("%Y-%m-%d %H:%M")


def app_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QBrush(QColor("#ffffff")))
    p.setPen(QPen(QColor("#37474f"), 3))
    p.drawRoundedRect(10, 4, 40, 54, 4, 4)
    p.setBrush(QBrush(QColor("#c62828")))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(4, 30, 34, 18, 3, 3)
    f = QFont("Segoe UI", 9, QFont.Bold)
    p.setFont(f)
    p.setPen(QColor("white"))
    p.drawText(4, 30, 34, 18, Qt.AlignCenter, "PDF")
    p.setPen(QPen(QColor("#1565c0"), 5, Qt.SolidLine, Qt.RoundCap))
    p.drawArc(34, 30, 26, 26, 30 * 16, 280 * 16)
    p.end()
    return QIcon(pm)


def badge(text: str, color: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"QLabel {{ background:{color}; color:white; border-radius:8px; padding:2px 8px; "
                      f"font-weight:600; }}")
    lbl.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Maximum)
    return lbl


def heading(text: str, sub: str | None = None) -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 8)
    t = QLabel(text)
    f = t.font()
    f.setPointSize(f.pointSize() + 6)
    f.setBold(True)
    t.setFont(f)
    lay.addWidget(t)
    if sub:
        s = QLabel(sub)
        s.setWordWrap(True)
        s.setStyleSheet("color: palette(placeholder-text);")
        lay.addWidget(s)
    return w


def card(title: str) -> tuple[QFrame, QVBoxLayout]:
    fr = QFrame()
    fr.setObjectName("card")
    fr.setStyleSheet("QFrame#card { border: 1px solid palette(mid); border-radius: 8px; }")
    lay = QVBoxLayout(fr)
    t = QLabel(title)
    t.setStyleSheet("font-weight:600;")
    lay.addWidget(t)
    return fr, lay


def info_box(text: str, kind: str = "info") -> QLabel:
    colors = {"info": ("#e3f2fd", "#0d47a1"), "warn": ("#fff3e0", "#8a4b00"), "error": ("#ffebee", "#b71c1c"),
              "ok": ("#e8f5e9", "#1b5e20")}
    bg, fg = colors[kind]
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setTextFormat(Qt.RichText)
    lbl.setStyleSheet(f"QLabel {{ background:{bg}; color:{fg}; border-radius:6px; padding:8px; }}")
    return lbl


def confirm(parent, title: str, text: str, ok_text: str = "Continue", danger: bool = False) -> bool:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning if danger else QMessageBox.Question)
    box.setWindowTitle(title)
    box.setText(text)
    ok = box.addButton(ok_text, QMessageBox.AcceptRole)
    box.addButton(QMessageBox.Cancel)
    box.setDefaultButton(QMessageBox.Cancel if danger else ok)
    box.exec()
    return box.clickedButton() is ok


def error(parent, text: str, title: str = "PDF Replacement Manager"):
    QMessageBox.critical(parent, title, text)


def reveal_in_explorer(path: Path | str):
    p = Path(path)
    if p.exists():
        subprocess.Popen(["explorer", "/select,", str(p)])  # noqa: S603,S607 - fixed program
    elif p.parent.exists():
        os.startfile(str(p.parent))


class ColorButton(QPushButton):
    changed = Signal(object)

    def __init__(self, color=None, allow_none: bool = True, parent=None):
        super().__init__(parent)
        self.allow_none = allow_none
        self.setColor(color)
        self.clicked.connect(self._pick)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda _: self._clear())

    def setColor(self, c):
        self._color = list(c) if c else None
        if self._color:
            q = QColor.fromRgbF(*self._color)
            self.setText(q.name())
            self.setStyleSheet(f"QPushButton {{ background:{q.name()}; color:{'black' if q.lightness() > 128 else 'white'};}}")
        else:
            self.setText("none" if self.allow_none else "pick…")
            self.setStyleSheet("")
        self.setToolTip("Click to choose; right-click to clear" if self.allow_none else "Click to choose")

    def color(self):
        return self._color

    def _pick(self):
        start = QColor.fromRgbF(*self._color) if self._color else QColor("black")
        c = QColorDialog.getColor(start, self, "Choose colour")
        if c.isValid():
            self.setColor([round(c.redF(), 4), round(c.greenF(), 4), round(c.blueF(), 4)])
            self.changed.emit(self._color)

    def _clear(self):
        if self.allow_none:
            self.setColor(None)
            self.changed.emit(None)


class ServiceSignals(QObject):
    """Thread-safe bridge from service callbacks (worker threads) to the GUI thread."""
    changed = Signal()
    notify = Signal(object, str, str)


def hbox(*widgets, stretch_last=False) -> QHBoxLayout:
    lay = QHBoxLayout()
    for w in widgets:
        if w is None:
            lay.addStretch(1)
        elif isinstance(w, QWidget):
            lay.addWidget(w)
        else:
            lay.addLayout(w)
    if stretch_last:
        lay.addStretch(1)
    return lay
