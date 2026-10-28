"""Reusable, replayable PDF editing operations.

An operation is a JSON-serialisable dict: {"id", "type", "enabled", ...fields}. The same list is replayed
on every matching download (operations mode) and drives the interactive editor.

Geometry is stored in *media coordinates*: unrotated, relative to the MediaBox top-left, in points.
Every operation declares its effect so the UI never presents an overlay as a genuine edit:

  content-replace  glyphs genuinely removed and new text written in the content stream
  content-removal  glyphs / pixels genuinely removed (true redaction or deletion)
  overlay          new content drawn on top; underlying content unchanged
  annotation       separate annotation object; underlying content unchanged
  structure        pages, bookmarks, form fields
  metadata         document information dictionary
"""
from __future__ import annotations

import copy
import html
import io
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import pymupdf

from .errors import OperationError
from .pdftext import BASE14, CSS_FAMILY, choose_font, find_text, page_glyphs, redaction_rect, repair_shifted_glyphs

# --------------------------------------------------------------------------- field specs


@dataclass
class F:
    kind: str                  # int,float,str,text,bool,color,choice,rect,pages,page,asset,json,points
    label: str
    default: object = None
    required: bool = False
    choices: tuple = ()
    help: str = ""
    advanced: bool = False


@dataclass
class OpSpec:
    type: str
    label: str
    category: str
    effect: str
    fields: dict
    apply: Callable
    transform: Callable | None = None
    geometric: bool = False      # has a 'rect' that can be moved/resized on the canvas
    explain: str = ""


REGISTRY: dict[str, OpSpec] = {}


def register(spec: OpSpec):
    REGISTRY[spec.type] = spec
    return spec


@dataclass
class OpResult:
    op_id: str
    type: str
    ok: bool
    effect: str
    count: int = 0
    verified: bool = True
    warnings: list = field(default_factory=list)
    details: dict = field(default_factory=dict)
    error: str | None = None

    def to_dict(self):
        return asdict(self)


@dataclass
class ApplyContext:
    asset_resolver: Callable[[str], Path] | None = None
    file_name: str = "document.pdf"
    download_ns: int | None = None
    processing_ns: int | None = None


# --------------------------------------------------------------------------- helpers
def new_op(op_type: str, **params) -> dict:
    spec = REGISTRY[op_type]
    op = {"id": uuid.uuid4().hex[:12], "type": op_type, "enabled": True}
    for name, f in spec.fields.items():
        op[name] = copy.deepcopy(f.default)
    op.update(params)
    return op


def parse_pages(spec, n: int) -> list[int]:
    """'all' | 'odd' | 'even' | 'last' | '1,3,5-7' | '2-' | '-3' (1-based) | int (0-based) -> 0-based list."""
    if spec is None or spec == "" or spec == "all":
        return list(range(n))
    if isinstance(spec, int):
        idx = [spec]
    elif isinstance(spec, list):
        idx = [int(x) for x in spec]
    else:
        s = str(spec).replace(" ", "").lower()
        if s == "odd":
            return list(range(0, n, 2))
        if s == "even":
            return list(range(1, n, 2))
        idx = []
        for part in s.split(","):
            if not part:
                continue
            if part == "last":
                idx.append(n - 1)
                continue
            m = re.fullmatch(r"(\d*)-(\d*|last)", part)
            if m:
                a = int(m.group(1)) if m.group(1) else 1
                b = n if m.group(2) in ("", "last") else int(m.group(2))
                idx.extend(i - 1 for i in range(a, b + 1))
            elif part.isdigit():
                idx.append(int(part) - 1)
            else:
                raise OperationError(f"Invalid page selection '{spec}'.")
    out = sorted(set(idx))
    bad = [i + 1 for i in out if i < 0 or i >= n]
    if bad:
        raise OperationError(f"Page(s) {bad} do not exist (document has {n} pages).")
    return out


def media_to_page(page: pymupdf.Page, rect) -> pymupdf.Rect:
    r = pymupdf.Rect(rect)
    o = page.cropbox_position
    return pymupdf.Rect(r.x0 - o.x, r.y0 - o.y, r.x1 - o.x, r.y1 - o.y)


def page_to_media(page: pymupdf.Page, rect) -> list[float]:
    r = pymupdf.Rect(rect)
    o = page.cropbox_position
    return [round(r.x0 + o.x, 2), round(r.y0 + o.y, 2), round(r.x1 + o.x, 2), round(r.y1 + o.y, 2)]


def _rgb(c, default=(0, 0, 0)):
    if c is None:
        return None if default is None else tuple(default)
    return tuple(float(x) for x in c)


def _hex(c) -> str:
    r, g, b = (int(round(x * 255)) for x in _rgb(c))
    return f"#{r:02x}{g:02x}{b:02x}"


def _get_page(doc, idx) -> pymupdf.Page:
    if not isinstance(idx, int) or idx < 0 or idx >= doc.page_count:
        raise OperationError(f"Page {idx + 1 if isinstance(idx, int) else idx} does not exist "
                             f"(document has {doc.page_count} pages).")
    return doc[idx]


def _resolve_asset(ctx: ApplyContext, asset_id: str) -> Path:
    if not asset_id:
        raise OperationError("No file selected for this operation.")
    if ctx.asset_resolver is None:
        raise OperationError("Asset storage is not available.")
    p = ctx.asset_resolver(asset_id)
    if not p or not Path(p).is_file():
        raise OperationError("A file used by this operation is missing from the profile's asset store.")
    return Path(p)


def _visual_point(page: pymupdf.Page, vx: float, vy: float) -> pymupdf.Point:
    """Point given in the rotated (as displayed) page space -> unrotated page coordinates."""
    return pymupdf.Point(vx, vy) * page.derotation_matrix


def expand_tokens(text: str, op: dict, ctx: ApplyContext, page_no: int, total: int) -> str:
    def date_token(m):
        name, fmt = m.group(1), m.group(2) or "%Y-%m-%d"
        if not op.get("authorized_dates"):
            raise OperationError("This text contains a dynamic date. Dynamic dates require explicit "
                                 "confirmation ('I am authorised to change dates in this document').")
        ns = ctx.download_ns if name == "download_date" else ctx.processing_ns
        ns = ns or __import__("time").time_ns()
        return datetime.fromtimestamp(ns / 1e9).strftime(fmt)

    out = re.sub(r"\{(download_date|processing_date)(?::([^}]*))?\}", date_token, text)
    return (out.replace("{page}", str(page_no)).replace("{total}", str(total))
               .replace("{filename}", ctx.file_name))


# --------------------------------------------------------------------------- text ops
def _target_hits(doc, op) -> list:
    pages = parse_pages(op.get("pages"), doc.page_count) if op.get("page") is None else [op["page"]]
    area = op.get("area")
    hits = []
    for pno in pages:
        page = _get_page(doc, pno)
        for h in find_text(page, op["find"], match_case=op.get("match_case", False),
                           whole_word=op.get("whole_word", False)):
            if area:
                a = media_to_page(page, area)
                a = pymupdf.Rect(a.x0 - 2, a.y0 - 2, a.x1 + 2, a.y1 + 2)
                if not a.contains(h.rect) and not a.intersects(h.rect):
                    continue
            hits.append(h)
    occ = op.get("occurrence", "all")
    if occ not in ("all", None, ""):
        k = int(occ)
        hits = hits[k - 1:k] if 0 < k <= len(hits) else []
    return hits


def _remove_glyphs(doc, page, rects, *, fill=None, images=pymupdf.PDF_REDACT_IMAGE_NONE,
                   graphics=pymupdf.PDF_REDACT_LINE_ART_NONE, font_policy="auto") -> dict:
    """Genuinely remove glyphs under rects, then put every surviving glyph back at its exact position."""
    before = page_glyphs(page)
    for r in rects:
        page.add_redact_annot(r, fill=fill if fill is not None else False, cross_out=False)
    page.apply_redactions(images=images, graphics=graphics, text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    report = repair_shifted_glyphs(doc, page, before, list(rects), font_policy)
    # verify: every surviving glyph is where it was before (±0.3 pt)
    after = page_glyphs(page)
    displaced = 0
    for key, glyphs in before.items():
        now = [(g.c, g.x) for g in after.get(key, [])]
        for g in glyphs:
            cx, cy = (g.bbox.x0 + g.bbox.x1) / 2, (g.bbox.y0 + g.bbox.y1) / 2
            if any(r.contains(pymupdf.Point(cx, cy)) for r in rects) or g.c.isspace():
                continue
            if not any(c == g.c and abs(x - g.x) <= 0.3 for c, x in now):
                displaced += 1
    report["displaced"] = displaced
    return report


def _redact_hits(doc, hits, font_policy="auto") -> dict:
    """Genuinely remove the glyphs of each hit (text only; images and vector art kept)."""
    by_page: dict[int, list] = {}
    for h in hits:
        by_page.setdefault(h.page, []).append(h)
    neighbours_ok, repaired, fonts = True, 0, set()
    for pno, hs in by_page.items():
        rep = _remove_glyphs(doc, doc[pno], [redaction_rect(h) for h in hs], font_policy=font_policy)
        neighbours_ok &= rep["displaced"] == 0
        repaired += rep["repaired"]
        fonts.update(rep["fonts"])
    return {"neighbours_ok": neighbours_ok, "repaired": repaired, "fonts": sorted(fonts)}


def apply_replace_text(doc, op, ctx) -> OpResult:
    res = OpResult(op["id"], op["type"], True, "content-replace")
    hits = _target_hits(doc, op)
    if not hits:
        if op.get("optional"):
            res.warnings.append("Text not found; skipped (operation marked optional).")
            return res
        raise OperationError(f"Text to replace was not found{_where(op)}.")
    if any(not h.horizontal for h in hits):
        raise OperationError("Replacing rotated/vertical text is not supported.")
    replacement = op.get("replace", "")
    check = _redact_hits(doc, hits, op.get("font_policy", "auto"))
    fonts_used = set()
    for h in hits:
        page = doc[h.page]
        size = float(op.get("size") or h.size)
        color = _rgb(op.get("color")) if op.get("color") else h.color
        if not replacement:
            continue
        fname, font, label = choose_font(doc, page, h, replacement, op.get("font_policy", "auto"))
        if not fname:
            raise OperationError(f"Cannot write replacement text: {label}.")
        fonts_used.add(label)
        width = font.text_length(replacement, fontsize=size)
        if op.get("fit", "shrink") == "shrink" and width > h.rect.width * 1.02 and h.rect.width > 0:
            size = max(size * 0.6, size * h.rect.width / width)
            if font.text_length(replacement, fontsize=size) > h.rect.width * 1.02:
                res.warnings.append("Replacement is wider than the original even at 60% size; it may overlap.")
        elif width > h.rect.width * 1.02:
            res.warnings.append("Replacement is wider than the original text and may overlap neighbours.")
        page.insert_text(h.origin, replacement, fontsize=size, fontname=fname, color=color)
    # verify
    for h in hits:
        if replacement:
            got = doc[h.page].get_textbox(h.line_rect + (-2, -2, max(40, h.rect.width * 2), 2))
            if "".join(replacement.split()) not in "".join(got.split()):
                res.verified = False
                res.warnings.append(f"Could not read back the replacement on page {h.page + 1}.")
    if not check["neighbours_ok"]:
        res.verified = False
        res.warnings.append("Neighbouring characters on the same line may have been affected.")
    res.count = len(hits)
    res.details = {"fonts": sorted(fonts_used), "pages": sorted({h.page + 1 for h in hits}),
                   "glyphs_repositioned": check["repaired"]}
    if any(lbl.startswith("substitute") for lbl in fonts_used):
        res.warnings.append("Original font could not be reused; a substitute font was used.")
    return res


def apply_delete_text(doc, op, ctx) -> OpResult:
    res = OpResult(op["id"], op["type"], True, "content-removal")
    hits = _target_hits(doc, op)
    if not hits:
        if op.get("optional"):
            res.warnings.append("Text not found; skipped.")
            return res
        raise OperationError(f"Text to delete was not found{_where(op)}.")
    check = _redact_hits(doc, hits)
    for h in hits:
        if op["find"].lower() in doc[h.page].get_textbox(h.rect).lower():
            res.verified = False
    if not check["neighbours_ok"]:
        res.verified = False
        res.warnings.append("Neighbouring characters on the same line may have been affected.")
    res.count = len(hits)
    return res


def _where(op):
    if op.get("page") is not None:
        return f" on page {op['page'] + 1}"
    return ""


def apply_add_text(doc, op, ctx) -> OpResult:
    res = OpResult(op["id"], op["type"], True, "overlay")
    page = _get_page(doc, op["page"])
    text = expand_tokens(op.get("text", ""), op, ctx, op["page"] + 1, doc.page_count)
    rect = media_to_page(page, op["rect"])
    fam = op.get("font", "helv")
    archive = None
    css_font = CSS_FAMILY.get(fam, "sans-serif")
    extra_css = ""
    if fam.startswith("asset:"):
        fp = _resolve_asset(ctx, fam[6:])
        archive = pymupdf.Archive(str(fp.parent))
        extra_css = f"@font-face {{font-family: prmcustom; src: url({fp.name});}}\n"
        css_font = "prmcustom"
    body = html.escape(text).replace("\n", "<br>")
    css = (extra_css + f"* {{font-family: {css_font}; font-size: {float(op.get('size', 12))}pt; "
           f"color: {_hex(op.get('color'))}; text-align: {op.get('align', 'left')}; "
           f"font-weight: {'bold' if op.get('bold') else 'normal'}; "
           f"font-style: {'italic' if op.get('italic') else 'normal'}; margin: 0; padding: 0;}}")
    if op.get("background"):
        page.draw_rect(rect, color=None, fill=_rgb(op["background"]), fill_opacity=float(op.get("opacity", 1)))
    spare, scale = page.insert_htmlbox(rect, body, css=css, archive=archive,
                                       scale_low=0 if op.get("fit") == "shrink" else 1,
                                       rotate=page.rotation, opacity=float(op.get("opacity", 1)))
    if spare < 0:
        raise OperationError("Text does not fit in its box. Enlarge the box, reduce the font size, "
                             "or enable 'shrink to fit'.")
    res.count = 1
    if scale < 1:
        res.warnings.append(f"Text was shrunk to {scale:.0%} to fit.")
    return res


def apply_ocr_layer(doc, op, ctx) -> OpResult:
    res = OpResult(op["id"], op["type"], True, "overlay")
    page = _get_page(doc, op["page"])
    font = pymupdf.Font("helv")
    for w in op.get("words", []):
        text, r = w[0], media_to_page(page, w[1:5])
        if not text.strip() or r.is_empty:
            continue
        size = max(1.0, r.height * 0.8)
        tl = font.text_length(text, fontsize=size)
        if tl > r.width and tl > 0:
            size *= r.width / tl
        page.insert_text(pymupdf.Point(r.x0, r.y1 - r.height * 0.2), text, fontsize=size, fontname="helv",
                         render_mode=3)
        res.count += 1
    res.details["note"] = "Invisible, searchable text layer; page appearance unchanged."
    return res


# --------------------------------------------------------------------------- redaction
def apply_redact(doc, op, ctx) -> OpResult:
    res = OpResult(op["id"], op["type"], True, "content-removal")
    fill = _rgb(op.get("fill"), (0, 0, 0))
    areas_by_page: dict[int, list] = {}
    for a in op.get("areas") or []:
        page = _get_page(doc, a["page"])
        areas_by_page.setdefault(a["page"], []).append(media_to_page(page, a["rect"]))
    terms = [t for t in (op.get("terms") or []) if t]
    for pno in parse_pages(op.get("pages"), doc.page_count) if terms else []:
        for t in terms:
            for h in find_text(doc[pno], t, match_case=op.get("match_case", False)):
                areas_by_page.setdefault(pno, []).append(h.rect)
    if not areas_by_page:
        if op.get("optional"):
            res.warnings.append("Nothing to redact was found.")
            return res
        raise OperationError("Nothing to redact was found (no areas, or search terms not present).")
    img = pymupdf.PDF_REDACT_IMAGE_PIXELS if op.get("remove_images", True) else pymupdf.PDF_REDACT_IMAGE_NONE
    art = pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED if op.get("remove_graphics", True) \
        else pymupdf.PDF_REDACT_LINE_ART_NONE
    for pno, rects in areas_by_page.items():
        page = doc[pno]
        _remove_glyphs(doc, page, rects, fill=fill, images=img, graphics=art)
        res.count += len(rects)
        for r in rects:  # verification: no text remains inside a redacted area
            inner = pymupdf.Rect(r.x0 + 0.5, r.y0 + 0.5, r.x1 - 0.5, r.y1 - 0.5)
            if page.get_textbox(inner).strip():
                res.verified = False
                res.warnings.append(f"Text still detected inside a redaction area on page {pno + 1}.")
        for t in terms:
            if find_text(page, t, match_case=op.get("match_case", False)):
                res.verified = False
                res.warnings.append(f"A redaction term is still present on page {pno + 1}.")
    if op.get("scrub_metadata"):
        doc.set_metadata({})
        try:
            doc.del_xml_metadata()
        except Exception:
            pass
        res.details["metadata"] = "Document metadata removed."
    res.details["pages"] = sorted(p + 1 for p in areas_by_page)
    return res


# --------------------------------------------------------------------------- annotations
_TEXT_MARKUP = {"highlight": "add_highlight_annot", "underline": "add_underline_annot",
                "strikeout": "add_strikeout_annot", "squiggly": "add_squiggly_annot"}


def apply_markup(doc, op, ctx) -> OpResult:
    res = OpResult(op["id"], op["type"], True, "annotation")
    groups: dict[int, list] = {}
    if op.get("find"):
        for h in _target_hits(doc, op):
            groups.setdefault(h.page, []).append(h.rect)
    else:
        page = _get_page(doc, op["page"])
        groups[op["page"]] = [media_to_page(page, r) for r in op.get("rects") or []]
    if not any(groups.values()):
        if op.get("optional"):
            return res
        raise OperationError("Nothing to mark: text not found / no area selected.")
    for pno, rects in groups.items():
        page = doc[pno]
        annot = getattr(page, _TEXT_MARKUP[op.get("kind", "highlight")])(rects)
        annot.set_colors(stroke=_rgb(op.get("color"), (1, 1, 0)))
        annot.set_opacity(float(op.get("opacity", 1)))
        if op.get("comment"):
            annot.set_info(content=op["comment"])
        annot.update()
        res.count += 1
    return res


def apply_note(doc, op, ctx) -> OpResult:
    page = _get_page(doc, op["page"])
    r = media_to_page(page, op["rect"])
    a = page.add_text_annot(r.tl, op.get("text", ""), icon=op.get("icon", "Note"))
    a.set_colors(stroke=_rgb(op.get("color"), (1, 0.8, 0)))
    if op.get("author"):
        a.set_info(title=op["author"])
    a.update()
    return OpResult(op["id"], op["type"], True, "annotation", 1)


def apply_comment(doc, op, ctx) -> OpResult:
    page = _get_page(doc, op["page"])
    r = media_to_page(page, op["rect"])
    a = page.add_freetext_annot(r, op.get("text", ""), fontsize=float(op.get("size", 11)),
                                text_color=_rgb(op.get("color")), fill_color=_rgb(op.get("fill"), None),
                                border_width=float(op.get("border_width", 0)),
                                opacity=float(op.get("opacity", 1)), rotate=page.rotation,
                                align={"left": 0, "center": 1, "right": 2}.get(op.get("align", "left"), 0))
    a.update()
    return OpResult(op["id"], op["type"], True, "annotation", 1)


def _norm_points(rect: pymupdf.Rect, pts):
    return [pymupdf.Point(rect.x0 + px * rect.width, rect.y0 + py * rect.height) for px, py in pts]


def apply_shape(doc, op, ctx) -> OpResult:
    page = _get_page(doc, op["page"])
    r = media_to_page(page, op["rect"])
    kind = op.get("kind", "rect")
    stroke = _rgb(op.get("color"), (1, 0, 0))
    fill = _rgb(op.get("fill"), None)
    width = float(op.get("width", 1.5))
    opacity = float(op.get("opacity", 1))
    pts = op.get("points") or [[0, 0], [1, 1]]
    if op.get("as_annotation", True):
        if kind == "rect":
            a = page.add_rect_annot(r)
        elif kind == "ellipse":
            a = page.add_circle_annot(r)
        elif kind in ("line", "arrow"):
            p1, p2 = _norm_points(r, pts[:2])
            a = page.add_line_annot(p1, p2)
            if kind == "arrow":
                a.set_line_ends(pymupdf.PDF_ANNOT_LE_NONE, pymupdf.PDF_ANNOT_LE_OPEN_ARROW)
        elif kind == "ink":
            a = page.add_ink_annot([[tuple(p) for p in _norm_points(r, s)] for s in op.get("strokes") or []])
        else:
            raise OperationError(f"Unknown shape '{kind}'.")
        a.set_border(width=width)
        a.set_colors(stroke=stroke, fill=fill if kind in ("rect", "ellipse") else None)
        a.set_opacity(opacity)
        a.update()
        return OpResult(op["id"], op["type"], True, "annotation", 1)
    shape = page.new_shape()
    if kind == "rect":
        shape.draw_rect(r)
    elif kind == "ellipse":
        shape.draw_oval(r)
    elif kind in ("line", "arrow"):
        p1, p2 = _norm_points(r, pts[:2])
        shape.draw_line(p1, p2)
    elif kind == "ink":
        for s in op.get("strokes") or []:
            shape.draw_polyline(_norm_points(r, s))
    shape.finish(color=stroke, fill=fill if kind in ("rect", "ellipse") else None, width=width,
                 stroke_opacity=opacity, fill_opacity=opacity)
    shape.commit()
    return OpResult(op["id"], op["type"], True, "overlay", 1)


# --------------------------------------------------------------------------- images / stamps
def _image_bytes(path: Path, crop, opacity: float) -> bytes:
    from PIL import Image
    with Image.open(path) as im:
        im.load()
        im = im.convert("RGBA")
        if crop and list(crop) != [0, 0, 1, 1]:
            w, h = im.size
            box = (int(crop[0] * w), int(crop[1] * h), int(crop[2] * w), int(crop[3] * h))
            if box[2] - box[0] < 1 or box[3] - box[1] < 1:
                raise OperationError("Image crop area is empty.")
            im = im.crop(box)
        if opacity < 1:
            alpha = im.getchannel("A").point(lambda a: int(a * opacity))
            im.putalpha(alpha)
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()


def apply_add_image(doc, op, ctx) -> OpResult:
    res = OpResult(op["id"], op["type"], True, "overlay")
    path = _resolve_asset(ctx, op.get("asset"))
    try:
        data = _image_bytes(path, op.get("crop"), float(op.get("opacity", 1)))
    except OperationError:
        raise
    except Exception as e:
        raise OperationError(f"Image could not be read ({type(e).__name__}).")
    pages = [op["page"]] if op.get("all_pages") is not True else list(range(doc.page_count))
    for pno in pages:
        page = _get_page(doc, pno)
        page.insert_image(media_to_page(page, op["rect"]), stream=data,
                          keep_proportion=bool(op.get("keep_proportion", True)),
                          rotate=int(op.get("rotate", 0)), overlay=not op.get("underlay", False))
        res.count += 1
    return res


def apply_watermark(doc, op, ctx) -> OpResult:
    res = OpResult(op["id"], op["type"], True, "overlay")
    font = pymupdf.Font(op.get("font", "helv") if op.get("font", "helv") in sum(BASE14.values(), ()) else "helv")
    size = float(op.get("size", 60))
    for pno in parse_pages(op.get("pages"), doc.page_count):
        page = doc[pno]
        text = expand_tokens(op.get("text", ""), op, ctx, pno + 1, doc.page_count)
        vr = page.rect  # displayed (rotated) size
        center = _visual_point(page, vr.width / 2, vr.height / 2)
        tw = pymupdf.TextWriter(page.rect, opacity=float(op.get("opacity", 0.25)), color=_rgb(op.get("color"),
                                                                                              (0.6, 0.6, 0.6)))
        w = font.text_length(text, fontsize=size)
        tw.append(pymupdf.Point(center.x - w / 2, center.y + size * 0.35), text, font=font, fontsize=size)
        tw.write_text(page, morph=(center, pymupdf.Matrix(float(op.get("angle", 45)) + page.rotation)),
                      overlay=not op.get("underlay", False))
        res.count += 1
    return res


def _place_text(page, text, position, margin, size, color, fontname="helv"):
    """Write upright single-line text at a named position in the displayed page orientation."""
    font = pymupdf.Font(fontname)
    vr = page.rect
    w = font.text_length(text, fontsize=size)
    vert, horiz = position.split("-")
    vx = {"left": margin, "center": (vr.width - w) / 2, "right": vr.width - margin - w}[horiz]
    vy = margin + size if vert == "top" else vr.height - margin
    page.insert_text(_visual_point(page, vx, vy), text, fontsize=size, fontname=fontname, color=color,
                     rotate=page.rotation)


def apply_page_numbers(doc, op, ctx) -> OpResult:
    res = OpResult(op["id"], op["type"], True, "overlay")
    pages = parse_pages(op.get("pages"), doc.page_count)
    start = int(op.get("start", 1))
    total = len(pages) + start - 1 if op.get("count_selected_only") else doc.page_count
    for k, pno in enumerate(pages):
        n = start + k if op.get("count_selected_only") else pno + start
        text = op.get("format", "Page {n} of {total}").replace("{n}", str(n)).replace("{total}", str(total))
        _place_text(doc[pno], text, op.get("position", "bottom-center"), float(op.get("margin", 24)),
                    float(op.get("size", 10)), _rgb(op.get("color")))
        res.count += 1
    return res


def apply_header_footer(doc, op, ctx) -> OpResult:
    res = OpResult(op["id"], op["type"], True, "overlay")
    for pno in parse_pages(op.get("pages"), doc.page_count):
        for key, vert in (("header", "top"), ("footer", "bottom")):
            t = op.get(key)
            if t:
                text = expand_tokens(t, op, ctx, pno + 1, doc.page_count)
                _place_text(doc[pno], text, f"{vert}-{op.get('align', 'center')}", float(op.get("margin", 24)),
                            float(op.get("size", 9)), _rgb(op.get("color")))
        res.count += 1
    return res


# --------------------------------------------------------------------------- pages
def apply_insert_pages(doc, op, ctx) -> OpResult:
    path = _resolve_asset(ctx, op.get("asset"))
    src = pymupdf.open(str(path))
    try:
        if src.needs_pass:
            raise OperationError("The PDF to insert is password protected.")
        sel = parse_pages(op.get("from_pages"), src.page_count)
        at = int(op.get("at", -1))
        if at > doc.page_count or at < -1:
            raise OperationError(f"Insert position {at + 1} is beyond the end of the document.")
        tmp = pymupdf.open()
        for i in sel:
            tmp.insert_pdf(src, from_page=i, to_page=i)
        doc.insert_pdf(tmp, start_at=at if at >= 0 else -1)
        n = len(sel)
    finally:
        src.close()
    if op.get("count") and int(op["count"]) != n:
        raise OperationError(f"Expected to insert {op['count']} page(s) but the source provides {n}.")
    return OpResult(op["id"], op["type"], True, "structure", n)


def apply_delete_pages(doc, op, ctx) -> OpResult:
    pages = parse_pages(op.get("pages"), doc.page_count) if op.get("pages") not in (None, "", "all") else None
    if not pages:
        raise OperationError("Select which pages to delete (deleting every page is not allowed).")
    if len(pages) >= doc.page_count:
        raise OperationError("Cannot delete every page of the document.")
    doc.delete_pages(pages)
    return OpResult(op["id"], op["type"], True, "structure", len(pages))


def apply_duplicate_page(doc, op, ctx) -> OpResult:
    p = op["page"]
    _get_page(doc, p)
    for _ in range(int(op.get("copies", 1))):
        doc.fullcopy_page(p, p + 1 if p + 1 < doc.page_count else -1)
    return OpResult(op["id"], op["type"], True, "structure", int(op.get("copies", 1)))


def apply_rotate_pages(doc, op, ctx) -> OpResult:
    angle = int(op.get("angle", 90))
    if angle % 90:
        raise OperationError("Rotation must be a multiple of 90 degrees.")
    pages = parse_pages(op.get("pages"), doc.page_count)
    for p in pages:
        doc[p].set_rotation((doc[p].rotation + angle) % 360)
    return OpResult(op["id"], op["type"], True, "structure", len(pages))


def apply_move_page(doc, op, ctx) -> OpResult:
    f, t = op["page"], int(op["to"])
    n = doc.page_count
    if not (0 <= f < n and 0 <= t < n):
        raise OperationError(f"Cannot move page {f + 1} to position {t + 1} (document has {n} pages).")
    order = list(range(n))
    order.insert(t, order.pop(f))
    doc.select(order)
    return OpResult(op["id"], op["type"], True, "structure", 1)


def apply_crop_pages(doc, op, ctx) -> OpResult:
    pages = parse_pages(op.get("pages"), doc.page_count)
    r = pymupdf.Rect(op["rect"])
    for p in pages:
        page = doc[p]
        mb = page.mediabox
        cr = pymupdf.Rect(max(r.x0, 0), max(r.y0, 0), min(r.x1, mb.width), min(r.y1, mb.height))
        if cr.is_empty or cr.width < 10 or cr.height < 10:
            raise OperationError(f"Crop area is outside page {p + 1}.")
        page.set_cropbox(cr)
    return OpResult(op["id"], op["type"], True, "structure", len(pages),
                    details={"note": "Cropping hides content outside the box; it is not removed."})


def apply_resize_pages(doc, op, ctx) -> OpResult:
    pages = parse_pages(op.get("pages"), doc.page_count)
    w, h = float(op["width"]), float(op["height"])
    if w < 36 or h < 36 or w > 14400 or h > 14400:
        raise OperationError("Page size must be between 36 and 14400 points.")
    for p in pages:
        page = doc[p]
        mb = page.mediabox  # PyMuPDF reports the mediabox top-left based
        raw = doc.xref_get_key(page.xref, "MediaBox")
        x0, y0, x1, y1 = [float(v) for v in re.findall(r"-?[\d.]+", raw[1])][:4] if raw[0] == "array" \
            else (0, 0, mb.width, mb.height)
        if op.get("anchor", "top-left") == "center":
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            new = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
        else:  # keep the visual top-left corner fixed (PDF y axis points up)
            new = (x0, y1 - h, x0 + w, y1)
        doc.xref_set_key(page.xref, "MediaBox", "[%g %g %g %g]" % new)
        doc.xref_set_key(page.xref, "CropBox", "null")
    return OpResult(op["id"], op["type"], True, "structure", len(pages))


# --------------------------------------------------------------------------- bookmarks / forms / metadata
def apply_add_bookmark(doc, op, ctx) -> OpResult:
    _get_page(doc, op["page"])
    toc = doc.get_toc(simple=False)
    level = max(1, int(op.get("level", 1)))
    if not toc:
        level = 1
    else:
        level = min(level, toc[-1][0] + 1)
    toc.append([level, op["title"], op["page"] + 1])
    doc.set_toc(toc)
    return OpResult(op["id"], op["type"], True, "structure", 1)


def apply_remove_bookmark(doc, op, ctx) -> OpResult:
    toc = doc.get_toc(simple=False)
    keep, removed, skip_level = [], 0, None
    for item in toc:
        if skip_level is not None and item[0] > skip_level:
            removed += 1
            continue
        skip_level = None
        if item[1] == op["title"]:
            removed += 1
            skip_level = item[0]
            continue
        keep.append(item)
    if not removed and not op.get("optional"):
        raise OperationError("Bookmark to remove was not found.")
    # re-level children of removed nodes is avoided by removing the subtree
    doc.set_toc(keep)
    return OpResult(op["id"], op["type"], True, "structure", removed)


def _all_widgets(doc):
    for page in doc:
        for w in page.widgets() or []:
            yield page, w


def apply_fill_field(doc, op, ctx) -> OpResult:
    res = OpResult(op["id"], op["type"], True, "structure")
    for page, w in _all_widgets(doc):
        if w.field_name == op["name"]:
            if w.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE:
                raise OperationError("Signature fields cannot be filled.")
            v = op.get("value")
            if w.field_type in (pymupdf.PDF_WIDGET_TYPE_CHECKBOX, pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON):
                on = str(v).lower() in ("1", "true", "yes", "on", "checked") or v is True
                w.field_value = w.on_state() if on else "Off"
            else:
                w.field_value = "" if v is None else str(v)
            w.update()
            res.count += 1
    if not res.count:
        raise OperationError(f"Form field '{op['name']}' was not found.")
    return res


_FIELD_TYPES = {"text": pymupdf.PDF_WIDGET_TYPE_TEXT, "checkbox": pymupdf.PDF_WIDGET_TYPE_CHECKBOX,
                "combobox": pymupdf.PDF_WIDGET_TYPE_COMBOBOX, "listbox": pymupdf.PDF_WIDGET_TYPE_LISTBOX}


def apply_add_field(doc, op, ctx) -> OpResult:
    page = _get_page(doc, op["page"])
    if not op.get("name"):
        raise OperationError("Form fields need a name.")
    if any(w.field_name == op["name"] for _, w in _all_widgets(doc)):
        raise OperationError(f"A form field named '{op['name']}' already exists.")
    w = pymupdf.Widget()
    w.field_type = _FIELD_TYPES[op.get("kind", "text")]
    w.field_name = op["name"]
    w.rect = media_to_page(page, op["rect"])
    w.text_fontsize = float(op.get("size", 0))
    choices = [c.strip() for c in (op.get("choices") or "").split(",") if c.strip()]
    if choices:
        w.choice_values = choices
    v = op.get("value")
    if w.field_type == pymupdf.PDF_WIDGET_TYPE_CHECKBOX:
        w.field_value = bool(v) and str(v).lower() not in ("0", "false", "off", "")
    elif v:
        w.field_value = str(v)
    page.add_widget(w)
    return OpResult(op["id"], op["type"], True, "structure", 1)


def apply_delete_field(doc, op, ctx) -> OpResult:
    n = 0
    for page, w in list(_all_widgets(doc)):
        if w.field_name == op["name"]:
            page.delete_widget(w)
            n += 1
    if not n:
        raise OperationError(f"Form field '{op['name']}' was not found.")
    return OpResult(op["id"], op["type"], True, "structure", n)


_META_KEYS = ("title", "author", "subject", "keywords")


def apply_set_metadata(doc, op, ctx) -> OpResult:
    meta = dict(doc.metadata or {})
    changed = []
    for k in _META_KEYS:
        if op.get(k) is not None and op.get(k) != meta.get(k):
            meta[k] = op[k]
            changed.append(k)
    # creation/modification dates are carried over unchanged
    doc.set_metadata({k: v for k, v in meta.items() if k in (*_META_KEYS, "creator", "producer", "creationDate",
                                                             "modDate", "trapped")})
    return OpResult(op["id"], op["type"], True, "metadata", len(changed), details={"changed": changed})


def apply_set_metadata_dates(doc, op, ctx) -> OpResult:
    if not op.get("authorized_dates"):
        raise OperationError("Changing internal PDF dates requires explicit authorisation on the operation.")
    meta = dict(doc.metadata or {})

    def conv(v):
        if v == "processing":
            dt = datetime.fromtimestamp((ctx.processing_ns or __import__("time").time_ns()) / 1e9).astimezone()
        else:
            dt = datetime.fromisoformat(v)
        return dt.strftime("D:%Y%m%d%H%M%S")

    changed = []
    for key, field_name in (("creationDate", "creation"), ("modDate", "modification")):
        if op.get(field_name):
            meta[key] = conv(op[field_name])
            changed.append(key)
    doc.set_metadata({k: v for k, v in meta.items() if k in (*_META_KEYS, "creator", "producer", "creationDate",
                                                             "modDate", "trapped")})
    return OpResult(op["id"], op["type"], True, "metadata", len(changed), details={"changed": changed})


# --------------------------------------------------------------------------- page transforms (editor mapping)
def _t_insert(op, tokens, new):
    at = int(op.get("at", -1))
    at = len(tokens) if at < 0 else min(at, len(tokens))
    return tokens[:at] + [new() for _ in range(int(op.get("count") or 1))] + tokens[at:]


def _t_delete(op, tokens, new):
    rm = set(parse_pages(op.get("pages"), len(tokens)))
    return [t for i, t in enumerate(tokens) if i not in rm]


def _t_duplicate(op, tokens, new):
    p = op["page"]
    return tokens[:p + 1] + [new() for _ in range(int(op.get("copies", 1)))] + tokens[p + 1:]


def _t_move(op, tokens, new):
    t = list(tokens)
    t.insert(int(op["to"]), t.pop(op["page"]))
    return t


# --------------------------------------------------------------------------- registry
COLOR_BLACK = [0, 0, 0]
_TEXT_TARGET = {
    "find": F("str", "Find text", "", True),
    "match_case": F("bool", "Match case", False),
    "whole_word": F("bool", "Whole words only", False),
    "page": F("page", "Only on page", None, help="Leave empty to search the pages below"),
    "pages": F("pages", "Pages", "all"),
    "area": F("rect", "Only inside area", None, advanced=True),
    "occurrence": F("str", "Occurrence (all or n)", "all"),
    "optional": F("bool", "Skip silently if not found", False, advanced=True),
}

register(OpSpec("replace_text", "Replace existing text", "Text", "content-replace", {
    **_TEXT_TARGET,
    "replace": F("str", "Replace with", ""),
    "font_policy": F("choice", "Font", "auto", choices=("auto", "original_only", "substitute"),
                     help="auto: reuse the embedded font if it has every glyph, else a close standard font"),
    "size": F("float", "Font size (blank = original)", None),
    "color": F("color", "Colour (blank = original)", None),
    "fit": F("choice", "If longer", "shrink", choices=("shrink", "keep")),
}, apply_replace_text, explain="Original glyphs are genuinely removed from the content stream (redaction "
   "engine, text only) and the replacement is written at the same baseline."))

register(OpSpec("delete_text", "Delete existing text", "Text", "content-removal", dict(_TEXT_TARGET),
                apply_delete_text, explain="Glyphs are genuinely removed; the space they occupied stays blank."))

register(OpSpec("add_text", "Add text box", "Text", "overlay", {
    "page": F("page", "Page", 0, True), "rect": F("rect", "Box", [72, 72, 300, 110], True),
    "text": F("text", "Text", "Text"), "font": F("choice", "Font", "helv", choices=("helv", "tiro", "cour")),
    "size": F("float", "Size", 12.0), "bold": F("bool", "Bold", False), "italic": F("bool", "Italic", False),
    "color": F("color", "Colour", COLOR_BLACK),
    "align": F("choice", "Align", "left", choices=("left", "center", "right", "justify")),
    "background": F("color", "Background", None), "opacity": F("float", "Opacity", 1.0),
    "fit": F("choice", "If too long", "error", choices=("error", "shrink")),
    "authorized_dates": F("bool", "I am authorised to insert dynamic dates", False, advanced=True,
                          help="Tokens {download_date:%Y-%m-%d} / {processing_date} change on every run"),
}, apply_add_text, geometric=True, explain="New text drawn on top of the page; wraps inside the box. "
   "Tokens: {page} {total} {filename}."))

register(OpSpec("ocr_text_layer", "OCR text layer", "Text", "overlay", {
    "page": F("page", "Page", 0, True), "words": F("json", "Words [text,x0,y0,x1,y1]", []),
}, apply_ocr_layer, explain="Adds invisible searchable text (from OCR, corrected by you)."))

register(OpSpec("redact", "Redact (secure)", "Redaction", "content-removal", {
    "areas": F("json", "Areas [{page, rect}]", []), "terms": F("json", "Search terms", []),
    "pages": F("pages", "Pages for terms", "all"), "match_case": F("bool", "Match case", False),
    "fill": F("color", "Fill", COLOR_BLACK), "remove_images": F("bool", "Remove image pixels", True),
    "remove_graphics": F("bool", "Remove covered vector art", True),
    "scrub_metadata": F("bool", "Also remove document metadata", False),
    "optional": F("bool", "Skip silently if nothing found", False, advanced=True),
}, apply_redact, explain="True redaction: text, image pixels and covered graphics under each area are "
   "permanently removed, then the area is filled."))

register(OpSpec("markup", "Highlight / underline / strike", "Annotate", "annotation", {
    "kind": F("choice", "Kind", "highlight", choices=tuple(_TEXT_MARKUP)),
    "find": F("str", "Text to mark (or use area)", ""), "match_case": F("bool", "Match case", False),
    "whole_word": F("bool", "Whole words", False),
    "page": F("page", "Page", None), "pages": F("pages", "Pages", "all"),
    "rects": F("json", "Areas", []), "occurrence": F("str", "Occurrence", "all"),
    "color": F("color", "Colour", [1, 1, 0]), "opacity": F("float", "Opacity", 1.0),
    "comment": F("text", "Comment", ""), "optional": F("bool", "Skip if not found", False, advanced=True),
}, apply_markup))

register(OpSpec("note", "Sticky note", "Annotate", "annotation", {
    "page": F("page", "Page", 0, True), "rect": F("rect", "Position", [72, 72, 92, 92], True),
    "text": F("text", "Note", ""), "author": F("str", "Author", ""),
    "icon": F("choice", "Icon", "Note", choices=("Note", "Comment", "Help", "Insert", "Key", "Paragraph")),
    "color": F("color", "Colour", [1, 0.8, 0]),
}, apply_note, geometric=True))

register(OpSpec("comment", "Comment box", "Annotate", "annotation", {
    "page": F("page", "Page", 0, True), "rect": F("rect", "Box", [72, 72, 250, 120], True),
    "text": F("text", "Text", "Comment"), "size": F("float", "Size", 11.0),
    "color": F("color", "Text colour", COLOR_BLACK), "fill": F("color", "Fill", [1, 1, 0.8]),
    "border_width": F("float", "Border", 1.0), "opacity": F("float", "Opacity", 1.0),
    "align": F("choice", "Align", "left", choices=("left", "center", "right")),
}, apply_comment, geometric=True))

register(OpSpec("shape", "Shape / drawing", "Draw", "annotation", {
    "page": F("page", "Page", 0, True), "rect": F("rect", "Bounds", [72, 72, 200, 150], True),
    "kind": F("choice", "Shape", "rect", choices=("rect", "ellipse", "line", "arrow", "ink")),
    "color": F("color", "Stroke", [1, 0, 0]), "fill": F("color", "Fill", None),
    "width": F("float", "Line width", 1.5), "opacity": F("float", "Opacity", 1.0),
    "points": F("json", "Line points (0..1)", [[0, 0], [1, 1]], advanced=True),
    "strokes": F("json", "Ink strokes (0..1)", [], advanced=True),
    "as_annotation": F("bool", "Keep as editable annotation", True,
                       help="Off = drawn permanently into the page content"),
}, apply_shape, geometric=True))

register(OpSpec("add_image", "Image", "Insert", "overlay", {
    "page": F("page", "Page", 0, True), "rect": F("rect", "Box", [72, 72, 200, 200], True),
    "asset": F("asset", "Image file", None, True), "keep_proportion": F("bool", "Keep proportions", True),
    "crop": F("json", "Crop (fractions x0,y0,x1,y1)", [0, 0, 1, 1]), "opacity": F("float", "Opacity", 1.0),
    "rotate": F("choice", "Rotate", 0, choices=(0, 90, 180, 270)),
    "all_pages": F("bool", "Repeat on every page", False), "underlay": F("bool", "Behind page content", False),
}, apply_add_image, geometric=True))

register(OpSpec("watermark", "Text watermark", "Insert", "overlay", {
    "text": F("str", "Text", "CONFIDENTIAL", True), "pages": F("pages", "Pages", "all"),
    "size": F("float", "Size", 60.0), "angle": F("float", "Angle", 45.0),
    "color": F("color", "Colour", [0.6, 0.6, 0.6]), "opacity": F("float", "Opacity", 0.25),
    "underlay": F("bool", "Behind page content", False),
}, apply_watermark))

register(OpSpec("page_numbers", "Page numbers", "Insert", "overlay", {
    "format": F("str", "Format", "Page {n} of {total}"), "pages": F("pages", "Pages", "all"),
    "position": F("choice", "Position", "bottom-center", choices=(
        "bottom-center", "bottom-right", "bottom-left", "top-center", "top-right", "top-left")),
    "start": F("int", "Start at", 1), "count_selected_only": F("bool", "Number only selected pages", False),
    "size": F("float", "Size", 10.0), "margin": F("float", "Margin", 24.0), "color": F("color", "Colour", COLOR_BLACK),
}, apply_page_numbers))

register(OpSpec("header_footer", "Header / footer", "Insert", "overlay", {
    "header": F("str", "Header", ""), "footer": F("str", "Footer", ""), "pages": F("pages", "Pages", "all"),
    "align": F("choice", "Align", "center", choices=("left", "center", "right")),
    "size": F("float", "Size", 9.0), "margin": F("float", "Margin", 24.0), "color": F("color", "Colour", COLOR_BLACK),
    "authorized_dates": F("bool", "I am authorised to insert dynamic dates", False, advanced=True),
}, apply_header_footer, explain="Tokens: {page} {total} {filename}."))

register(OpSpec("insert_pages", "Insert pages from PDF", "Pages", "structure", {
    "asset": F("asset", "PDF file", None, True), "from_pages": F("pages", "Pages of that file", "all"),
    "at": F("int", "Insert before page index (-1 = end)", -1), "count": F("int", "Page count", None, advanced=True),
}, apply_insert_pages, _t_insert))
register(OpSpec("delete_pages", "Delete pages", "Pages", "structure", {
    "pages": F("pages", "Pages", "", True)}, apply_delete_pages, _t_delete))
register(OpSpec("duplicate_page", "Duplicate page", "Pages", "structure", {
    "page": F("page", "Page", 0, True), "copies": F("int", "Copies", 1)}, apply_duplicate_page, _t_duplicate))
register(OpSpec("rotate_pages", "Rotate pages", "Pages", "structure", {
    "pages": F("pages", "Pages", "all"), "angle": F("choice", "Angle", 90, choices=(90, 180, 270))},
    apply_rotate_pages))
register(OpSpec("move_page", "Move page", "Pages", "structure", {
    "page": F("page", "Page", 0, True), "to": F("int", "New position (0-based)", 0, True)},
    apply_move_page, _t_move))
register(OpSpec("crop_pages", "Crop pages", "Pages", "structure", {
    "pages": F("pages", "Pages", "all"), "rect": F("rect", "Visible area", [36, 36, 576, 756], True)},
    apply_crop_pages, explain="Sets the CropBox; content outside is hidden, not deleted."))
register(OpSpec("resize_pages", "Change page size", "Pages", "structure", {
    "pages": F("pages", "Pages", "all"), "width": F("float", "Width (pt)", 612.0, True),
    "height": F("float", "Height (pt)", 792.0, True),
    "anchor": F("choice", "Anchor", "top-left", choices=("top-left", "center"))},
    apply_resize_pages, explain="Changes the MediaBox; content is not scaled."))
register(OpSpec("add_bookmark", "Add bookmark", "Document", "structure", {
    "title": F("str", "Title", "Bookmark", True), "page": F("page", "Target page", 0, True),
    "level": F("int", "Level", 1)}, apply_add_bookmark))
register(OpSpec("remove_bookmark", "Remove bookmark", "Document", "structure", {
    "title": F("str", "Title", "", True), "optional": F("bool", "Skip if missing", False)},
    apply_remove_bookmark))
register(OpSpec("fill_field", "Fill form field", "Forms", "structure", {
    "name": F("str", "Field name", "", True), "value": F("str", "Value", "")}, apply_fill_field))
register(OpSpec("add_field", "Add form field", "Forms", "structure", {
    "page": F("page", "Page", 0, True), "rect": F("rect", "Box", [72, 72, 250, 92], True),
    "kind": F("choice", "Type", "text", choices=tuple(_FIELD_TYPES)), "name": F("str", "Name", "", True),
    "value": F("str", "Default value", ""), "choices": F("str", "Choices (comma separated)", ""),
    "size": F("float", "Font size (0 = auto)", 0.0)}, apply_add_field, geometric=True))
register(OpSpec("delete_field", "Delete form field", "Forms", "structure", {
    "name": F("str", "Field name", "", True)}, apply_delete_field))
register(OpSpec("set_metadata", "Document properties", "Document", "metadata", {
    "title": F("str", "Title", None), "author": F("str", "Author", None),
    "subject": F("str", "Subject", None), "keywords": F("str", "Keywords", None)}, apply_set_metadata,
    explain="Edits title/author/subject/keywords only. Internal creation/modification dates are preserved."))
register(OpSpec("set_metadata_dates", "Internal PDF dates (advanced)", "Document", "metadata", {
    "creation": F("str", "CreationDate (ISO or 'processing')", ""),
    "modification": F("str", "ModDate (ISO or 'processing')", ""),
    "authorized_dates": F("bool", "I am authorised to change this document's internal dates", False, True)},
    apply_set_metadata_dates, explain="Changes dates stored INSIDE the PDF. These are separate from file "
    "timestamps. Only use where you are authorised to do so."))


# --------------------------------------------------------------------------- validation & replay
def validate_op(op: dict) -> list[str]:
    errs = []
    spec = REGISTRY.get(op.get("type"))
    if not spec:
        return [f"Unknown operation type {op.get('type')!r}."]
    for name, f in spec.fields.items():
        v = op.get(name)
        if f.required and (v is None or v == "" or v == []):
            if not (spec.type in ("replace_text", "delete_text") and name == "page"):
                errs.append(f"'{f.label}' is required.")
            continue
        if v is None:
            continue
        try:
            if f.kind == "page" and (not isinstance(v, int) or v < 0):
                errs.append(f"'{f.label}' must be a page.")
            elif f.kind == "rect":
                r = [float(x) for x in v]
                if len(r) != 4 or r[2] <= r[0] or r[3] <= r[1]:
                    errs.append(f"'{f.label}' must be a non-empty rectangle.")
            elif f.kind == "color":
                c = [float(x) for x in v]
                if len(c) != 3 or any(x < 0 or x > 1 for x in c):
                    errs.append(f"'{f.label}' must be an RGB colour.")
            elif f.kind in ("float", "int"):
                float(v)
                if name == "opacity" and not 0 <= float(v) <= 1:
                    errs.append("Opacity must be between 0 and 1.")
                if name == "size" and float(v) < 0:
                    errs.append("Size cannot be negative.")
            elif f.kind == "choice" and f.choices and v not in f.choices:
                errs.append(f"'{f.label}' must be one of {', '.join(map(str, f.choices))}.")
            elif f.kind == "pages" and v not in ("", None):
                parse_pages(v, 100000)
        except (TypeError, ValueError, OperationError):
            errs.append(f"'{f.label}' has an invalid value.")
    if spec.type == "redact" and not (op.get("areas") or op.get("terms")):
        errs.append("Add at least one redaction area or search term.")
    if spec.type == "markup" and not (op.get("find") or op.get("rects")):
        errs.append("Give text to find or select an area.")
    if spec.type == "markup" and op.get("rects") and op.get("page") is None:
        errs.append("Area markup needs a page.")
    return errs


def simulate_pages(n: int, ops: list[dict]) -> int:
    tokens = list(range(n))
    counter = [n]

    def new():
        counter[0] += 1
        return counter[0]
    for op in ops:
        spec = REGISTRY.get(op.get("type"))
        if op.get("enabled", True) and spec and spec.transform:
            tokens = spec.transform(op, tokens, new)
    return len(tokens)


def apply_operations(doc: pymupdf.Document, ops: list[dict], ctx: ApplyContext | None = None,
                     stop_on_error: bool = True) -> list[OpResult]:
    ctx = ctx or ApplyContext()
    results = []
    for op in ops:
        if not op.get("enabled", True):
            continue
        spec = REGISTRY.get(op.get("type"))
        errs = validate_op(op)
        if errs:
            r = OpResult(op.get("id", "?"), op.get("type", "?"), False, spec.effect if spec else "?",
                         error="; ".join(errs))
            results.append(r)
            if stop_on_error:
                break
            continue
        try:
            results.append(spec.apply(doc, op, ctx))
        except OperationError as e:
            results.append(OpResult(op["id"], op["type"], False, spec.effect, error=e.user_message))
            if stop_on_error:
                break
        except Exception as e:  # library failure on odd input; keep message generic (no content)
            results.append(OpResult(op["id"], op["type"], False, spec.effect,
                                    error=f"{spec.label} failed ({type(e).__name__}: {str(e)[:120]})"))
            if stop_on_error:
                break
    return results


def describe(op: dict) -> str:
    spec = REGISTRY.get(op.get("type"))
    if not spec:
        return op.get("type", "?")
    t = op["type"]
    pg = f" · p.{op['page'] + 1}" if isinstance(op.get("page"), int) else ""
    short = lambda s: (s[:28] + "…") if s and len(s) > 29 else (s or "")  # noqa: E731
    if t == "replace_text":
        return f"Replace “{short(op.get('find'))}” → “{short(op.get('replace'))}”{pg}"
    if t == "delete_text":
        return f"Delete “{short(op.get('find'))}”{pg}"
    if t == "add_text":
        return f"Text “{short(op.get('text'))}”{pg}"
    if t == "shape":
        return f"{op.get('kind', 'shape').title()}{pg}"
    if t == "markup":
        return f"{op.get('kind', 'highlight').title()} {('“' + short(op['find']) + '”') if op.get('find') else 'area'}{pg}"
    if t == "redact":
        return f"Redact {len(op.get('areas') or [])} area(s), {len(op.get('terms') or [])} term(s)"
    if t in ("delete_pages", "rotate_pages", "crop_pages", "resize_pages"):
        return f"{spec.label} ({op.get('pages')})"
    if t == "move_page":
        return f"Move page {op['page'] + 1} → position {int(op['to']) + 1}"
    if t in ("fill_field", "delete_field"):
        return f"{spec.label}: {op.get('name')}"
    return spec.label + pg
