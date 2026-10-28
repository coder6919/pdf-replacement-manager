"""Glyph-level text search and font helpers used by genuine text replacement/removal."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import pymupdf

BASE14 = {  # family -> (regular, bold, italic, bold-italic)
    "helv": ("helv", "hebo", "heit", "hebi"),
    "tiro": ("tiro", "tibo", "tiit", "tibi"),
    "cour": ("cour", "cobo", "coit", "cobi"),
}
CSS_FAMILY = {"helv": "sans-serif", "tiro": "serif", "cour": "monospace"}
WINDOWS_UNICODE_FONTS = [r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\segoeui.ttf",
                         r"C:\Windows\Fonts\calibri.ttf"]


@dataclass
class Hit:
    page: int
    rect: pymupdf.Rect             # page coords, union of matched glyph boxes
    origin: pymupdf.Point          # baseline origin of first glyph
    font: str
    size: float
    color: tuple
    flags: int
    line_rect: pymupdf.Rect
    line_text: str                 # kept in memory only, never logged/persisted
    start: int
    end: int
    horizontal: bool = True
    char_rects: list = field(default_factory=list)


def int_to_rgb(c: int) -> tuple:
    return ((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255


def find_text(page: pymupdf.Page, needle: str, *, match_case: bool = False,
              whole_word: bool = False) -> list[Hit]:
    """Find needle within single lines, returning glyph geometry and the style of the first glyph."""
    if not needle:
        return []
    hits: list[Hit] = []
    n = needle if match_case else needle.lower()
    raw = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_PRESERVE_LIGATURES)
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            chars = []
            for span in line["spans"]:
                for ch in span["chars"]:
                    chars.append((ch["c"], ch["bbox"], ch["origin"], span))
            text = "".join(c[0] for c in chars)
            hay = text if match_case else text.lower()
            start = 0
            while True:
                i = hay.find(n, start)
                if i < 0:
                    break
                j = i + len(n)
                start = j
                if whole_word and ((i > 0 and hay[i - 1].isalnum()) or (j < len(hay) and hay[j].isalnum())):
                    continue
                rects = [pymupdf.Rect(c[1]) for c in chars[i:j]]
                r = pymupdf.Rect(rects[0])
                for x in rects[1:]:
                    r |= x
                span = chars[i][3]
                dx, dy = line.get("dir", (1, 0))
                hits.append(Hit(page.number, r, pymupdf.Point(chars[i][2]), span["font"], span["size"],
                                int_to_rgb(span["color"]), span["flags"], pymupdf.Rect(line["bbox"]), text,
                                i, j, abs(dx - 1) < 1e-3 and abs(dy) < 1e-3, rects))
    return hits


@dataclass
class Glyph:
    c: str
    x: float
    y: float
    bbox: pymupdf.Rect
    font: str
    size: float
    color: tuple
    flags: int


def page_glyphs(page: pymupdf.Page) -> dict[float, list[Glyph]]:
    """All horizontal glyphs grouped by baseline (rounded), each list sorted left to right."""
    out: dict[float, list[Glyph]] = {}
    raw = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_PRESERVE_LIGATURES)
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            dx, dy = line.get("dir", (1, 0))
            if abs(dx - 1) > 1e-3 or abs(dy) > 1e-3:
                continue
            for span in line["spans"]:
                for ch in span["chars"]:
                    key = round(ch["origin"][1], 1)
                    out.setdefault(key, []).append(Glyph(ch["c"], ch["origin"][0], ch["origin"][1],
                                                         pymupdf.Rect(ch["bbox"]), span["font"], span["size"],
                                                         int_to_rgb(span["color"]), span["flags"]))
    for v in out.values():
        v.sort(key=lambda g: g.x)
    return out


def repair_shifted_glyphs(doc, page: pymupdf.Page, before: dict[float, list[Glyph]],
                          removed: list[pymupdf.Rect], font_policy: str = "auto") -> dict:
    """Undo MuPDF's redaction side effect where glyphs following a removed run (same text object)
    slide left by the advance of the last removed glyph.

    Glyphs that survived but moved are removed from their shifted position and re-written at their
    original baseline origin (original embedded font when possible). Returns a report.
    """
    def is_removed(g: Glyph) -> bool:
        cx, cy = (g.bbox.x0 + g.bbox.x1) / 2, (g.bbox.y0 + g.bbox.y1) / 2
        return any(r.contains(pymupdf.Point(cx, cy)) for r in removed)

    removed_ys = {k for k, gl in before.items() if any(is_removed(g) for g in gl)}
    after = page_glyphs(page)
    moved_runs = []   # (original glyphs, current rects)
    unaligned = 0
    for key in removed_ys:
        kept = [g for g in before[key] if not is_removed(g)]
        now = after.get(key, [])
        if [g.c for g in kept] != [g.c for g in now]:
            unaligned += 1
            continue
        run_o, run_n = [], []
        for o, n in zip(kept, now):
            if abs(o.x - n.x) > 0.05:
                run_o.append(o)
                run_n.append(n)
            elif run_o:
                moved_runs.append((run_o, run_n))
                run_o, run_n = [], []
        if run_o:
            moved_runs.append((run_o, run_n))
    if not moved_runs:
        return {"repaired": 0, "unaligned_lines": unaligned, "fonts": []}
    for _o, n in moved_runs:
        for g in n:
            page.add_redact_annot(pymupdf.Rect(g.bbox.x0 + 0.2, g.bbox.y0 + 0.2 * g.bbox.height,
                                               g.bbox.x1 - 0.2, g.bbox.y1 - 0.2 * g.bbox.height), fill=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                          text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    fonts = set()
    for o, _n in moved_runs:
        # split the run into style-homogeneous pieces and write each at its original origin
        piece = [o[0]]
        for g in o[1:] + [None]:
            if g is not None and (g.font, g.size, g.color, g.flags) == (piece[0].font, piece[0].size,
                                                                         piece[0].color, piece[0].flags):
                piece.append(g)
                continue
            text = "".join(x.c for x in piece)
            pseudo = Hit(page.number, pymupdf.Rect(piece[0].bbox), pymupdf.Point(piece[0].x, piece[0].y),
                         piece[0].font, piece[0].size, piece[0].color, piece[0].flags,
                         pymupdf.Rect(piece[0].bbox), text, 0, len(text), True, [p.bbox for p in piece])
            fname, _font, label = choose_font(doc, page, pseudo, text.strip() or "a", font_policy)
            if fname:
                page.insert_text(pseudo.origin, text, fontsize=piece[0].size, fontname=fname,
                                 color=piece[0].color)
                fonts.add(label)
            piece = [g] if g is not None else []
    return {"repaired": sum(len(o) for o, _ in moved_runs), "unaligned_lines": unaligned, "fonts": sorted(fonts)}


def redaction_rect(hit: Hit) -> pymupdf.Rect:
    """Slightly inset glyph union so neighbouring glyphs/lines are not caught by the redaction."""
    r = pymupdf.Rect(hit.rect)
    h = r.height
    first_w = hit.char_rects[0].width if hit.char_rects else 2
    last_w = hit.char_rects[-1].width if hit.char_rects else 2
    return pymupdf.Rect(r.x0 + min(1.0, 0.15 * first_w), r.y0 + 0.2 * h,
                        r.x1 - min(1.0, 0.15 * last_w), r.y1 - 0.2 * h)


def _font_entry(page: pymupdf.Page, span_font: str):
    for f in page.get_fonts(full=True):
        xref, basefont = f[0], f[3]
        if basefont.split("+")[-1] == span_font or basefont == span_font:
            return f
    return None


def base14_for(flags: int, family: str | None = None) -> str:
    fam = family or ("cour" if flags & 8 else "tiro" if flags & 4 else "helv")
    bold, italic = bool(flags & 16), bool(flags & 2)
    return BASE14[fam][(1 if bold else 0) + (2 if italic else 0)]


def _covers(font: pymupdf.Font, text: str) -> bool:
    return all(font.has_glyph(ord(ch)) for ch in text if not ch.isspace())


def unicode_fallback_font() -> str | None:
    return next((p for p in WINDOWS_UNICODE_FONTS if os.path.isfile(p)), None)


def choose_font(doc: pymupdf.Document, page: pymupdf.Page, hit: Hit, text: str, policy: str = "auto"):
    """Return (fontname_to_use, pymupdf.Font, label). Inserts the font resource into the page if needed.

    'auto': reuse the embedded original font if it is extractable and contains every glyph needed,
            otherwise fall back to the closest standard font. 'original_only': fail instead of substituting.
    """
    if policy in ("auto", "original_only"):
        entry = _font_entry(page, hit.font)
        if entry is not None:
            try:
                _name, ext, _type, buf = doc.extract_font(entry[0])
                if buf and ext not in ("n/a", ""):
                    f = pymupdf.Font(fontbuffer=buf)
                    if _covers(f, text):
                        alias = f"PRMo{entry[0]}"
                        page.insert_font(fontname=alias, fontbuffer=buf)
                        return alias, f, f"original embedded font ({hit.font})"
                elif entry[1] == "n/a" and entry[2] == "Type1":
                    # non-embedded standard font: the base-14 equivalent is the same font
                    std = _base14_from_name(entry[3])
                    if std and all(ord(c) < 256 for c in text):
                        return std, pymupdf.Font(std), f"original standard font ({entry[3]})"
            except Exception:
                pass
        if policy == "original_only":
            return None, None, "original font unavailable or missing glyphs"
    if all(ord(c) < 256 for c in text):
        name = base14_for(hit.flags)
        return name, pymupdf.Font(name), f"substitute standard font ({name})"
    path = unicode_fallback_font()
    if not path:
        return None, None, "no font covering the replacement text"
    alias = "PRMuni"
    page.insert_font(fontname=alias, fontfile=path)
    return alias, pymupdf.Font(fontfile=path), f"substitute font ({os.path.basename(path)})"


def _base14_from_name(name: str) -> str | None:
    n = name.lower()
    fam = "cour" if "courier" in n else "tiro" if "times" in n else "helv" if ("helvetica" in n or "arial" in n) else None
    if not fam:
        return None
    bold, italic = "bold" in n, ("italic" in n or "oblique" in n)
    return BASE14[fam][(1 if bold else 0) + (2 if italic else 0)]


def words_in_rect(page: pymupdf.Page, rect: pymupdf.Rect) -> list[pymupdf.Rect]:
    """Word boxes intersecting rect (for highlight/underline by drag)."""
    out = []
    for w in page.get_text("words"):
        r = pymupdf.Rect(w[:4])
        if r.intersects(rect):
            out.append(r)
    return out


def selected_line_text(page: pymupdf.Page, rect: pymupdf.Rect) -> tuple[str, pymupdf.Rect] | tuple[None, str]:
    """Contiguous text on ONE line whose glyph centres fall inside rect (for 'edit existing text').

    Returns (text, bbox) or (None, reason).
    """
    raw = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_PRESERVE_LIGATURES)
    found = []
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            chars = [c for s in line["spans"] for c in s["chars"]]
            idx = [i for i, c in enumerate(chars)
                   if rect.contains(pymupdf.Point((c["bbox"][0] + c["bbox"][2]) / 2, (c["bbox"][1] + c["bbox"][3]) / 2))]
            if idx:
                found.append((chars, idx))
    if not found:
        return None, "No text there. Drag across the text you want to change (or click a word)."
    if len(found) > 1:
        return None, "The selection spans several lines. Select text on one line at a time."
    chars, idx = found[0]
    sel = chars[idx[0]: idx[-1] + 1]
    text = "".join(c["c"] for c in sel).strip()
    if not text:
        return None, "Only whitespace selected."
    bbox = pymupdf.Rect(sel[0]["bbox"])
    for c in sel[1:]:
        bbox |= pymupdf.Rect(c["bbox"])
    return text, bbox


def word_at(page: pymupdf.Page, point: pymupdf.Point) -> pymupdf.Rect | None:
    for w in page.get_text("words"):
        r = pymupdf.Rect(w[:4])
        if r.contains(point):
            return r
    return None
