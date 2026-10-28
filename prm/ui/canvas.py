"""Interactive page canvas: renders a page and lets the user create, select, move and resize objects.

Coordinates: PDF page space (unrotated, points) <-> display space via page.rotation_matrix <-> scene (* zoom).
"""
from __future__ import annotations

import pymupdf
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QImage, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsLineItem, QGraphicsPathItem, QGraphicsPixmapItem,
                               QGraphicsRectItem, QGraphicsScene, QGraphicsView)

DRAG_TOOLS = {"text", "comment", "rect", "ellipse", "image", "redact", "field", "crop", "highlight", "underline",
              "strikeout", "edit_text"}
CLICK_TOOLS = {"note"}
LINE_TOOLS = {"line", "arrow"}


def render_page(page: pymupdf.Page, zoom: float, dpr: float = 1.0) -> QPixmap:
    m = pymupdf.Matrix(zoom * dpr, zoom * dpr)
    pix = page.get_pixmap(matrix=m, alpha=False, annots=True)
    img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
    pm = QPixmap.fromImage(img)
    pm.setDevicePixelRatio(dpr)
    return pm


class _Handle(QGraphicsRectItem):
    SIZE = 9

    def __init__(self, parent):
        super().__init__(0, 0, self.SIZE, self.SIZE, parent)
        self.setBrush(QBrush(QColor("#1565c0")))
        self.setPen(QPen(Qt.white))
        self.setCursor(Qt.SizeFDiagCursor)
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations, False)
        self._start = None

    def mousePressEvent(self, e):
        self._start = (e.scenePos(), QRectF(self.parentItem().rect()))
        e.accept()

    def mouseMoveEvent(self, e):
        if not self._start:
            return
        p0, r0 = self._start
        d = e.scenePos() - p0
        r = QRectF(0, 0, max(8.0, r0.width() + d.x()), max(8.0, r0.height() + d.y()))
        self.parentItem().prepareGeometryChange()
        self.parentItem().setRect(r)
        self.parentItem().place_handle()

    def mouseReleaseEvent(self, e):
        self._start = None
        self.parentItem().commit()


class ObjectItem(QGraphicsRectItem):
    """Outline of an editable object (an operation with a rectangle)."""

    def __init__(self, view: "PageView", op_id: str, scene_rect: QRectF, label: str):
        super().__init__(0, 0, scene_rect.width(), scene_rect.height())
        self.view, self.op_id = view, op_id
        self.setPos(scene_rect.topLeft())
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemIsSelectable |
                      QGraphicsItem.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self.setToolTip(label)
        self.setPen(QPen(QColor("#1565c0"), 1, Qt.DashLine))
        self.setBrush(QBrush(QColor(21, 101, 192, 18)))
        self.handle = _Handle(self)
        self.place_handle()
        self._moved = False

    def place_handle(self):
        r = self.rect()
        self.handle.setPos(r.right() - _Handle.SIZE / 2, r.bottom() - _Handle.SIZE / 2)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            self._moved = True
        if change == QGraphicsItem.ItemSelectedHasChanged:
            self.setPen(QPen(QColor("#1565c0"), 2 if value else 1, Qt.SolidLine if value else Qt.DashLine))
            self.handle.setVisible(bool(value))
        return super().itemChange(change, value)

    def mouseReleaseEvent(self, e):
        super().mouseReleaseEvent(e)
        if self._moved:
            self._moved = False
            self.commit()

    def commit(self):
        self.view.objectGeometryChanged.emit(self.op_id, self.view.scene_to_page(self.mapRectToScene(self.rect())))


class PageView(QGraphicsView):
    objectSelected = Signal(object)                      # op_id or None
    objectGeometryChanged = Signal(str, object)          # op_id, pymupdf.Rect (page coords)
    createRequested = Signal(str, object, object)        # tool, pymupdf.Rect, extra dict
    zoomRequested = Signal(int)
    deleteRequested = Signal(str)

    def __init__(self, parent=None, interactive: bool = True):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(self.renderHints())
        self.setDragMode(QGraphicsView.NoDrag)
        self.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.setBackgroundBrush(QBrush(QColor("#e8eaed")))
        self.interactive = interactive
        self.tool = "select"
        self.zoom = 1.0
        self.rot = pymupdf.Matrix(1, 0, 0, 1, 0, 0)
        self.derot = pymupdf.Matrix(1, 0, 0, 1, 0, 0)
        self._drag_start: QPointF | None = None
        self._temp = None
        self._ink: list[QPointF] = []
        self._objects: dict[str, ObjectItem] = {}
        self._highlights: list = []
        self.page_size = QRectF()
        self._rebuilding = False
        self.scene().selectionChanged.connect(self._sel_changed)

    # ------------------------------------------------------------------ mapping
    def page_to_scene(self, r) -> QRectF:
        d = pymupdf.Rect(r) * self.rot
        d.normalize()
        return QRectF(d.x0 * self.zoom, d.y0 * self.zoom, d.width * self.zoom, d.height * self.zoom)

    def scene_to_page(self, q: QRectF) -> pymupdf.Rect:
        d = pymupdf.Rect(q.left() / self.zoom, q.top() / self.zoom, q.right() / self.zoom, q.bottom() / self.zoom)
        r = d * self.derot
        r.normalize()
        return r

    def scene_point_to_page(self, p: QPointF) -> pymupdf.Point:
        return pymupdf.Point(p.x() / self.zoom, p.y() / self.zoom) * self.derot

    # ------------------------------------------------------------------ content
    def show_page(self, page: pymupdf.Page, zoom: float, objects=(), selected: str | None = None):
        self.zoom = zoom
        self.rot = pymupdf.Matrix(page.rotation_matrix)
        self.derot = pymupdf.Matrix(page.derotation_matrix)
        sc = self.scene()
        self._rebuilding = True
        sc.clear()
        self._objects.clear()
        self._highlights = []
        dpr = self.devicePixelRatioF()
        pm = render_page(page, zoom, dpr)
        item = QGraphicsPixmapItem(pm)
        item.setZValue(-10)
        sc.addItem(item)
        self.page_size = QRectF(0, 0, pm.width() / dpr, pm.height() / dpr)
        shadow = sc.addRect(self.page_size.translated(3, 3), QPen(Qt.NoPen), QBrush(QColor(0, 0, 0, 40)))
        shadow.setZValue(-11)
        sc.setSceneRect(self.page_size.adjusted(-20, -20, 20, 20))
        if self.interactive:
            for op, rect in objects:
                it = ObjectItem(self, op["id"], self.page_to_scene(rect), op.get("type", ""))
                sc.addItem(it)
                self._objects[op["id"]] = it
                if op["id"] == selected:
                    it.setSelected(True)
                else:
                    it.handle.setVisible(False)
        self._rebuilding = False

    def set_highlights(self, rects, current: int = -1):
        for h in self._highlights:
            self.scene().removeItem(h)
        self._highlights = []
        for i, r in enumerate(rects):
            it = self.scene().addRect(self.page_to_scene(r), QPen(QColor("#f57c00"), 2 if i == current else 0),
                                      QBrush(QColor(255, 193, 7, 110 if i == current else 60)))
            it.setZValue(5)
            self._highlights.append(it)
        if 0 <= current < len(rects):
            self.ensureVisible(self.page_to_scene(rects[current]), 50, 50)

    def select(self, op_id: str | None):
        for oid, it in self._objects.items():
            it.setSelected(oid == op_id)

    def _sel_changed(self):
        if self._rebuilding:
            return
        sel = [i for i in self.scene().selectedItems() if isinstance(i, ObjectItem)]
        self.objectSelected.emit(sel[0].op_id if sel else None)

    # ------------------------------------------------------------------ interaction
    def set_tool(self, tool: str):
        self.tool = tool
        self.viewport().setCursor(Qt.ArrowCursor if tool == "select" else
                                  Qt.IBeamCursor if tool == "edit_text" else Qt.CrossCursor)
        for it in self._objects.values():
            it.setFlag(QGraphicsItem.ItemIsMovable, tool == "select")
            it.setFlag(QGraphicsItem.ItemIsSelectable, tool == "select")

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            self.zoomRequested.emit(1 if e.angleDelta().y() > 0 else -1)
            e.accept()
        else:
            super().wheelEvent(e)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            sel = [i for i in self.scene().selectedItems() if isinstance(i, ObjectItem)]
            if sel:
                self.deleteRequested.emit(sel[0].op_id)
                return
        super().keyPressEvent(e)

    def _clamp(self, p: QPointF) -> QPointF:
        return QPointF(min(max(p.x(), 0), self.page_size.width()), min(max(p.y(), 0), self.page_size.height()))

    def mousePressEvent(self, e):
        if not self.interactive or self.tool == "select" or e.button() != Qt.LeftButton:
            return super().mousePressEvent(e)
        p = self._clamp(self.mapToScene(e.position().toPoint()))
        self._drag_start = p
        if self.tool == "ink":
            self._ink = [p]
            self._temp = self.scene().addPath(QPainterPath(p), QPen(QColor("#c62828"), 2))
        elif self.tool in LINE_TOOLS:
            self._temp = self.scene().addLine(p.x(), p.y(), p.x(), p.y(), QPen(QColor("#c62828"), 2))
        else:
            self._temp = self.scene().addRect(QRectF(p, p), QPen(QColor("#1565c0"), 1, Qt.DashLine),
                                              QBrush(QColor(21, 101, 192, 30)))
        e.accept()

    def mouseMoveEvent(self, e):
        if self._drag_start is None:
            return super().mouseMoveEvent(e)
        p = self._clamp(self.mapToScene(e.position().toPoint()))
        if isinstance(self._temp, QGraphicsPathItem):
            self._ink.append(p)
            path = self._temp.path()
            path.lineTo(p)
            self._temp.setPath(path)
        elif isinstance(self._temp, QGraphicsLineItem):
            s = self._drag_start
            self._temp.setLine(s.x(), s.y(), p.x(), p.y())
        elif self._temp is not None:
            self._temp.setRect(QRectF(self._drag_start, p).normalized())

    def mouseReleaseEvent(self, e):
        if self._drag_start is None:
            return super().mouseReleaseEvent(e)
        p = self._clamp(self.mapToScene(e.position().toPoint()))
        s, self._drag_start = self._drag_start, None
        if self._temp is not None:
            self.scene().removeItem(self._temp)
            self._temp = None
        tool = self.tool
        if tool == "ink":
            pts = [self.scene_point_to_page(q) for q in self._ink]
            if len(pts) >= 2:
                xs, ys = [q.x for q in pts], [q.y for q in pts]
                r = pymupdf.Rect(min(xs), min(ys), max(xs), max(ys))
                r = pymupdf.Rect(r.x0, r.y0, max(r.x1, r.x0 + 1), max(r.y1, r.y0 + 1))
                strokes = [[[round((q.x - r.x0) / r.width, 4), round((q.y - r.y0) / r.height, 4)] for q in pts]]
                self.createRequested.emit(tool, r, {"strokes": strokes})
            return
        if tool in LINE_TOOLS:
            a, b = self.scene_point_to_page(s), self.scene_point_to_page(p)
            r = pymupdf.Rect(min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y))
            r = pymupdf.Rect(r.x0, r.y0, max(r.x1, r.x0 + 1), max(r.y1, r.y0 + 1))
            pts = [[round((q.x - r.x0) / r.width, 4), round((q.y - r.y0) / r.height, 4)] for q in (a, b)]
            if abs(a - b) > 3:
                self.createRequested.emit(tool, r, {"points": pts})
            return
        sr = QRectF(s, p).normalized()
        click = sr.width() < 4 and sr.height() < 4
        self.createRequested.emit(tool, self.scene_to_page(sr), {"click": click,
                                                                  "point": self.scene_point_to_page(p)})
