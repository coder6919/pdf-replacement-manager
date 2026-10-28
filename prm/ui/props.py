"""Properties panel generated from an operation's field specification."""
from __future__ import annotations

import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from ..operations import REGISTRY
from .common import EFFECT_TEXT, ColorButton, badge


class PropertiesPanel(QScrollArea):
    """Edits one operation. Emits changed(op_id, {field: value}) on every committed edit."""
    changed = Signal(str, dict)
    assetRequested = Signal(str, str)     # op_id, field name

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.op = None
        self.page_count = 1
        self.show_advanced = False
        self.result = None
        self._build_empty()

    def _build_empty(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lbl = QLabel("Select an object on the page or an operation in the list to edit its properties.\n\n"
                     "Pick a tool in the toolbar to add new content.")
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
        lay.addStretch(1)
        self.setWidget(w)

    def set_op(self, op: dict | None, page_count: int = 1, result=None):
        self.op, self.page_count, self.result = op, page_count, result
        if op is None:
            self._build_empty()
            return
        spec = REGISTRY[op["type"]]
        w = QWidget()
        outer = QVBoxLayout(w)
        title = QLabel(f"<b>{spec.label}</b>")
        outer.addWidget(title)
        name, color, explanation = EFFECT_TEXT.get(spec.effect, (spec.effect, "#455a64", ""))
        if op["type"] == "shape" and not op.get("as_annotation", True):
            name, color, explanation = EFFECT_TEXT["overlay"]
        row = QHBoxLayout()
        row.addWidget(badge(name, color))
        row.addStretch(1)
        outer.addLayout(row)
        ex = QLabel(explanation + (" " + spec.explain if spec.explain else ""))
        ex.setWordWrap(True)
        ex.setStyleSheet("color: palette(placeholder-text);")
        outer.addWidget(ex)
        if result is not None:
            if not result.ok:
                e = QLabel(f"⚠ {result.error}")
                e.setWordWrap(True)
                e.setStyleSheet("color:#c62828; font-weight:600;")
                outer.addWidget(e)
            for warn in result.warnings:
                lw = QLabel(f"• {warn}")
                lw.setWordWrap(True)
                lw.setStyleSheet("color:#b26a00;")
                outer.addWidget(lw)
            if result.details.get("fonts"):
                outer.addWidget(QLabel("Font: " + ", ".join(result.details["fonts"])))
        form = QFormLayout()
        has_adv = False
        for fname, f in spec.fields.items():
            if (f.advanced or f.kind == "rect") and not self.show_advanced:
                has_adv = True
                continue
            widget = self._editor_for(fname, f, op.get(fname))
            if widget is None:
                continue
            if f.help:
                widget.setToolTip(f.help)
            form.addRow(f.label, widget)
        outer.addLayout(form)
        en = QCheckBox("Enabled")
        en.setChecked(op.get("enabled", True))
        en.toggled.connect(lambda v: self._emit("enabled", v))
        outer.addWidget(en)
        if has_adv or self.show_advanced:
            adv = QCheckBox("Show advanced fields")
            adv.setChecked(self.show_advanced)
            adv.toggled.connect(self._toggle_adv)
            outer.addWidget(adv)
        outer.addStretch(1)
        self.setWidget(w)

    def _toggle_adv(self, v):
        self.show_advanced = v
        self.set_op(self.op, self.page_count, self.result)

    def _emit(self, field, value):
        if self.op is not None and self.op.get(field) != value:
            self.changed.emit(self.op["id"], {field: value})

    def _editor_for(self, name, f, value):
        k = f.kind
        if k == "bool":
            w = QCheckBox()
            w.setChecked(bool(value))
            w.toggled.connect(lambda v, n=name: self._emit(n, v))
            return w
        if k == "int":
            w = QSpinBox()
            w.setRange(-1, 100000)
            w.setValue(int(value) if value is not None else -1 if name == "at" else 0)
            w.editingFinished.connect(lambda w=w, n=name: self._emit(n, w.value()))
            return w
        if k == "float":
            w = QDoubleSpinBox()
            w.setDecimals(2)
            w.setRange(0, 14400)
            w.setSingleStep(0.05 if name == "opacity" else 1)
            if name == "opacity":
                w.setRange(0, 1)
            w.setSpecialValueText("original" if value is None and f.default is None else "")
            w.setValue(float(value) if value is not None else 0)
            w.editingFinished.connect(lambda w=w, n=name: self._emit(n, w.value() if w.value() or f.default
                                                                     is not None else None))
            return w
        if k == "page":
            w = QSpinBox()
            optional = not f.required
            w.setRange(0 if optional else 1, max(1, self.page_count))
            if optional:
                w.setSpecialValueText("any")
            w.setValue((value + 1) if isinstance(value, int) else 0)
            w.editingFinished.connect(lambda w=w, n=name: self._emit(n, (w.value() - 1) if w.value() else None))
            return w
        if k in ("str", "pages"):
            w = QLineEdit("" if value is None else str(value))
            if k == "pages":
                w.setPlaceholderText("all, 1-3, 5, last, odd, even")
            w.editingFinished.connect(lambda w=w, n=name: self._emit(
                n, w.text() if (w.text() or f.default not in (None,)) else None))
            return w
        if k == "text":
            box = QWidget()
            lay = QVBoxLayout(box)
            lay.setContentsMargins(0, 0, 0, 0)
            t = QPlainTextEdit(value or "")
            t.setFixedHeight(80)
            b = QPushButton("Apply text")
            b.clicked.connect(lambda _=False, t=t, n=name: self._emit(n, t.toPlainText()))
            lay.addWidget(t)
            lay.addWidget(b)
            return box
        if k == "color":
            w = ColorButton(value, allow_none=f.default is None or name in ("fill", "background", "color"))
            w.changed.connect(lambda c, n=name: self._emit(n, c))
            return w
        if k == "choice":
            w = QComboBox()
            for c in f.choices:
                w.addItem(str(c), c)
            idx = w.findData(value)
            w.setCurrentIndex(max(0, idx))
            w.currentIndexChanged.connect(lambda i, w=w, n=name: self._emit(n, w.itemData(i)))
            return w
        if k == "rect":
            if not self.show_advanced:     # move/resize on the canvas; exact numbers are an advanced option
                return None
            box = QWidget()
            lay = QHBoxLayout(box)
            lay.setContentsMargins(0, 0, 0, 0)
            spins = []
            for v in (value or [0, 0, 0, 0]):
                s = QDoubleSpinBox()
                s.setRange(-14400, 14400)
                s.setDecimals(1)
                s.setValue(float(v))
                spins.append(s)
                lay.addWidget(s)
            for s in spins:
                s.editingFinished.connect(lambda n=name, sp=spins: self._emit(n, [round(x.value(), 2) for x in sp]))
            return box
        if k == "asset":
            b = QPushButton("Choose file…" if not value else f"{value[:10]}… (change)")
            b.clicked.connect(lambda _=False, n=name: self.assetRequested.emit(self.op["id"], n))
            return b
        if k == "json":
            if not self.show_advanced and name in ("points", "strokes", "words", "rects"):
                return None
            w = QLineEdit(json.dumps(value))

            def commit(w=w, n=name):
                try:
                    self._emit(n, json.loads(w.text() or "null"))
                    w.setStyleSheet("")
                except json.JSONDecodeError:
                    w.setStyleSheet("border:1px solid #c62828;")
            w.editingFinished.connect(commit)
            return w
        return None
