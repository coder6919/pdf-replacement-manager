"""PDF inspection and privacy-preserving content fingerprints.

PDFs are untrusted input: they are opened read-only by MuPDF (no JavaScript execution) and nothing
from their text is persisted — fingerprints contain only hashes.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field

import pymupdf

pymupdf.TOOLS.mupdf_display_errors(False)
pymupdf.TOOLS.mupdf_display_warnings(False)

SHINGLE_K = 256
_WORD = re.compile(r"\w+", re.UNICODE)


@dataclass
class PdfInfo:
    ok: bool
    error: str | None = None
    page_count: int = 0
    needs_password: bool = False
    encrypted: bool = False
    signed: bool = False
    signature_fields: int = 0
    has_forms: bool = False
    form_fields: int = 0
    has_annotations: bool = False
    has_bookmarks: bool = False
    has_links: bool = False
    has_javascript: bool = False
    has_embedded_files: bool = False
    repaired: bool = False
    scanned_pages: list[int] = field(default_factory=list)
    page_sizes: list[list[float]] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def open_pdf(src) -> pymupdf.Document:
    """Open from path or bytes. Raises on unreadable input."""
    if isinstance(src, (bytes, bytearray)):
        return pymupdf.open(stream=bytes(src), filetype="pdf")
    return pymupdf.open(str(src), filetype="pdf")


def inspect_doc(doc: pymupdf.Document) -> PdfInfo:
    info = PdfInfo(ok=True)
    info.needs_password = bool(doc.needs_pass)
    info.encrypted = bool(doc.is_encrypted) or bool(doc.needs_pass) or bool(
        (doc.metadata or {}).get("encryption"))
    if info.needs_password:
        info.warnings.append("Password protected: contents cannot be read without the password.")
        return info
    info.page_count = doc.page_count
    info.repaired = bool(getattr(doc, "is_repaired", False))
    info.metadata = {k: v for k, v in (doc.metadata or {}).items() if v}
    try:
        info.signed = doc.get_sigflags() > 0
    except Exception:
        info.signed = False
    try:
        info.has_bookmarks = bool(doc.get_toc(simple=True))
    except Exception:
        pass
    try:
        info.has_embedded_files = doc.embfile_count() > 0
    except Exception:
        pass
    try:
        root = doc.pdf_catalog()
        names = doc.xref_get_key(root, "Names")
        info.has_javascript = "JavaScript" in (doc.xref_object(root) or "") or (
            names[0] != "null" and "JavaScript" in names[1])
    except Exception:
        pass
    for i, page in enumerate(doc):
        r = page.rect
        if i < 50:
            info.page_sizes.append([round(r.width, 1), round(r.height, 1)])
        widgets = list(page.widgets() or [])
        info.form_fields += len(widgets)
        info.signature_fields += sum(1 for w in widgets if w.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE)
        if any(a.type[0] != pymupdf.PDF_ANNOT_WIDGET for a in page.annots() or []):
            info.has_annotations = True
        if page.first_link:
            info.has_links = True
        if i < 200 and not page.get_text("text").strip() and page.get_images(full=False):
            info.scanned_pages.append(i)
    info.has_forms = info.form_fields > 0
    if info.signature_fields and not info.signed:
        info.warnings.append("Contains signature fields.")
    if info.signed:
        info.warnings.append("Digitally signed: any change to the document invalidates the signature.")
    if info.encrypted:
        info.warnings.append("Encrypted (permissions/owner password).")
    if info.scanned_pages:
        info.warnings.append(f"{len(info.scanned_pages)} page(s) look scanned (image only, no text layer).")
    if info.has_javascript:
        info.warnings.append("Contains JavaScript (never executed by this application).")
    if info.repaired:
        info.warnings.append("File structure was damaged and repaired on open.")
    return info


def inspect_pdf(src) -> PdfInfo:
    try:
        doc = open_pdf(src)
    except Exception as e:  # corrupt / not a PDF
        return PdfInfo(ok=False, error=f"Not a readable PDF ({type(e).__name__}).")
    try:
        if doc.page_count == 0 and not doc.needs_pass:
            return PdfInfo(ok=False, error="PDF has no pages.")
        return inspect_doc(doc)
    except Exception as e:
        return PdfInfo(ok=False, error=f"PDF could not be inspected ({type(e).__name__}).")
    finally:
        doc.close()


# ------------------------------------------------------------------ fingerprints
def _h64(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")


def _dhash(page: pymupdf.Page) -> str:
    zoom = 32 / max(page.rect.width, 1)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False)
    from PIL import Image
    img = Image.frombytes("L", (pix.width, pix.height), pix.samples).resize((9, 8))
    px = list(img.tobytes())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return f"{bits:016x}"


def content_fingerprint(doc: pymupdf.Document, max_pages: int = 20) -> dict:
    words: list[str] = []
    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        words.extend(w.lower() for w in _WORD.findall(page.get_text("text")))
    hashes = sorted({_h64(" ".join(words[i:i + 3])) for i in range(max(0, len(words) - 2))})[:SHINGLE_K]
    return {
        "v": 1,
        "pages": doc.page_count,
        "sizes": [[round(p.rect.width), round(p.rect.height)] for p in list(doc)[:10]],
        "shingles": [f"{h:016x}" for h in hashes],
        "words": len(words),
        "dhash": _dhash(doc[0]) if doc.page_count else None,
    }


def fingerprint_file(src) -> dict:
    doc = open_pdf(src)
    try:
        return content_fingerprint(doc)
    finally:
        doc.close()


def similarity(a: dict | None, b: dict | None) -> float:
    """0..1 estimate of how similar two fingerprints are (text, structure, appearance)."""
    if not a or not b:
        return 0.0
    pa, pb = a.get("pages", 0), b.get("pages", 0)
    structure = (min(pa, pb) / max(pa, pb)) if max(pa, pb) else 0.0
    sa, sb = a.get("sizes") or [], b.get("sizes") or []
    if sa and sb:
        n = min(len(sa), len(sb))
        same = sum(1 for i in range(n) if abs(sa[i][0] - sb[i][0]) <= 2 and abs(sa[i][1] - sb[i][1]) <= 2)
        structure = 0.5 * structure + 0.5 * (same / max(len(sa), len(sb)))
    visual = 0.0
    if a.get("dhash") and b.get("dhash"):
        visual = 1 - bin(int(a["dhash"], 16) ^ int(b["dhash"], 16)).count("1") / 64
    A, B = set(a.get("shingles") or []), set(b.get("shingles") or [])
    if A and B:
        union_k = sorted(A | B)[:SHINGLE_K]
        text = sum(1 for h in union_k if h in A and h in B) / len(union_k)
        return round(0.7 * text + 0.15 * structure + 0.15 * visual, 4)
    if A or B:  # one has text, the other none
        return round(0.5 * structure * 0.5 + 0.25 * visual, 4)
    return round(0.5 * structure + 0.5 * visual, 4)
