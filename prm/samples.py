"""Synthetic sample PDFs for tests and the packaged self-test. Contains no real documents."""
from __future__ import annotations

import io
from pathlib import Path

import pymupdf


def report_pdf(path: Path | str | None = None, *, variant: int = 0, pages: int = 3,
               customer: str = "ACME Corporation", total: str = "1,234.56") -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 72), f"Monthly Report - Page {i + 1}", fontsize=18, fontname="hebo")
        page.insert_text((72, 110), f"Customer: {customer}", fontsize=12, fontname="helv")
        page.insert_text((72, 130), f"Invoice Total: {total} USD", fontsize=12, fontname="helv")
        page.insert_text((72, 150), "Reference code ABC-123 is confidential", fontsize=11, fontname="tiro")
        page.draw_rect(pymupdf.Rect(72, 170, 540, 172), color=(0, 0, 0.5), fill=(0, 0, 0.5))
        body = (f"Section {i + 1}. This synthetic paragraph exists to give the document enough words "
                f"for fingerprinting. Variant {variant}. Lorem ipsum dolor sit amet, consectetur "
                f"adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.")
        page.insert_textbox(pymupdf.Rect(72, 190, 540, 400), body, fontsize=11, fontname="helv")
    doc.set_toc([[1, "Start", 1], [1, "End", pages]])
    doc.set_metadata({"title": "Monthly Report", "author": "Sample Generator",
                      "creationDate": "D:20200101120000", "modDate": "D:20200102120000"})
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    if path:
        Path(path).write_bytes(data)
    return data


def different_pdf(path: Path | str | None = None) -> bytes:
    doc = pymupdf.open()
    for i in range(5):
        page = doc.new_page(width=595, height=842)
        page.insert_textbox(pymupdf.Rect(50, 50, 545, 800),
                            f"Completely unrelated recipe collection page {i}. Flour sugar eggs butter "
                            f"oven bake minutes golden brown serve warm with tea and jam. " * 3,
                            fontsize=12)
    data = doc.tobytes()
    doc.close()
    if path:
        Path(path).write_bytes(data)
    return data


def form_pdf(path: Path | str | None = None) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Application form", fontsize=16)
    w = pymupdf.Widget()
    w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    w.field_name = "full_name"
    w.rect = pymupdf.Rect(72, 100, 300, 120)
    page.add_widget(w)
    c = pymupdf.Widget()
    c.field_type = pymupdf.PDF_WIDGET_TYPE_CHECKBOX
    c.field_name = "agree"
    c.rect = pymupdf.Rect(72, 130, 86, 144)
    page.add_widget(c)
    data = doc.tobytes()
    doc.close()
    if path:
        Path(path).write_bytes(data)
    return data


def encrypted_pdf(path: Path | str | None = None, user_pw: str = "secret") -> bytes:
    doc = pymupdf.open(stream=report_pdf(), filetype="pdf")
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=user_pw, owner_pw=user_pw + "-owner")
    doc.close()
    if path:
        Path(path).write_bytes(data)
    return data


def signed_like_pdf(path: Path | str | None = None) -> bytes:
    """A PDF carrying a signature field with a /Sig value and SigFlags=3 (structure of a signed file).

    No cryptographic signature is generated; this exercises detection and refusal logic.
    """
    doc = pymupdf.open(stream=report_pdf(), filetype="pdf")
    page = doc[0]
    w = pymupdf.Widget()
    w.field_type = pymupdf.PDF_WIDGET_TYPE_SIGNATURE
    w.field_name = "Signature1"
    w.rect = pymupdf.Rect(400, 700, 550, 750)
    page.add_widget(w)
    sig = doc.get_new_xref()
    doc.update_object(sig, "<< /Type /Sig /Filter /Adobe.PPKLite /SubFilter /adbe.pkcs7.detached "
                           "/ByteRange [0 0 0 0] /Contents <00> >>")
    for wid in page.widgets():
        if wid.field_name == "Signature1":
            doc.xref_set_key(wid.xref, "V", f"{sig} 0 R")
    doc.xref_set_key(doc.pdf_catalog(), "AcroForm/SigFlags", "3")
    data = doc.tobytes()
    doc.close()
    if path:
        Path(path).write_bytes(data)
    return data


def corrupt_pdf(path: Path | str | None = None) -> bytes:
    data = b"%PDF-1.7\n1 0 obj << /Type /Catalog /Pages 2 0 R >>\n" + b"\x00garbage" * 50
    if path:
        Path(path).write_bytes(data)
    return data


def scanned_pdf(path: Path | str | None = None) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(pymupdf.Rect(50, 50, 550, 750), stream=png_image(400, 600, (240, 240, 240)))
    data = doc.tobytes()
    doc.close()
    if path:
        Path(path).write_bytes(data)
    return data


def png_image(w: int = 120, h: int = 80, color=(200, 30, 30), path: Path | str | None = None) -> bytes:
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (w, h), color)
    ImageDraw.Draw(im).rectangle([w // 4, h // 4, 3 * w // 4, 3 * h // 4], fill=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    if path:
        Path(path).write_bytes(buf.getvalue())
    return buf.getvalue()
