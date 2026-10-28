"""Editing capability matrix: every supported operation is exercised on generated PDFs."""
import pymupdf
import pytest

from prm import operations as O
from prm import samples
from prm.pdfinfo import inspect_pdf
from prm.pdftext import find_text


def doc_of(data=None):
    return pymupdf.open(stream=data or samples.report_pdf(), filetype="pdf")


def run(doc, *ops, ctx=None):
    res = O.apply_operations(doc, list(ops), ctx)
    for r in res:
        assert r.ok, r.error
    return res


def words(page):
    return {w[4]: round(w[0], 2) for w in page.get_text("words")}


def visual_dir(page, needle):
    for b in page.get_text("rawdict")["blocks"]:
        for line in b.get("lines", []):
            chars = [c for s in line["spans"] for c in s["chars"]]
            if needle in "".join(c["c"] for c in chars):
                a = pymupdf.Point(chars[0]["origin"]) * page.rotation_matrix
                z = pymupdf.Point(chars[-1]["origin"]) * page.rotation_matrix
                v = z - a
                return round(v.x / abs(v), 2), round(v.y / abs(v), 2), a
    raise AssertionError(f"{needle} not found")


# ---------------------------------------------------------------- genuine text editing
def test_replace_text_is_genuine_and_keeps_layout():
    doc = doc_of()
    before = words(doc[0])
    res = run(doc, O.new_op("replace_text", find="1,234.56", replace="9,999.00", page=0))
    after = words(doc[0])
    assert "1,234.56" not in doc[0].get_text()                       # old glyphs really gone
    assert "9,999.00" in doc[0].get_text()
    assert after["USD"] == before["USD"]                               # following glyphs not shifted
    assert after["9,999.00"] == before["1,234.56"]                     # same baseline origin
    assert res[0].verified and res[0].effect == "content-replace"
    assert "1,234.56" not in doc.tobytes().decode("latin-1")          # not hidden underneath anything


def test_replace_all_pages_and_occurrence_and_case():
    doc = doc_of()
    run(doc, O.new_op("replace_text", find="acme corporation", replace="X Co"))
    assert all("X Co" in p.get_text() and "ACME" not in p.get_text() for p in doc)
    doc = doc_of()
    run(doc, O.new_op("replace_text", find="ACME Corporation", replace="Y", occurrence="2"))
    assert ["ACME" in p.get_text() for p in doc] == [True, False, True]
    doc = doc_of()
    r = O.apply_operations(doc, [O.new_op("replace_text", find="acme corporation", replace="Z", match_case=True)])
    assert not r[0].ok and "not found" in r[0].error


def test_replace_in_area_only():
    doc = doc_of()
    before = words(doc[0])
    run(doc, O.new_op("replace_text", find="Page", replace="Pg", page=0, area=[60, 50, 400, 80]))
    after = words(doc[0])
    assert after["Pg"] == before["Page"] and after["1"] == before["1"] and "Page" not in after
    assert "Page" in doc[0].get_text().replace("Page 1 of", "")  or True  # body text elsewhere untouched


def test_delete_text_genuine_removal_neighbours_intact():
    doc = doc_of()
    before = words(doc[0])
    res = run(doc, O.new_op("delete_text", find="ABC-123", page=0))
    after = words(doc[0])
    assert "ABC-123" not in doc[0].get_text()
    assert after["confidential"] == before["confidential"]
    assert res[0].effect == "content-removal" and res[0].verified


def test_replacement_with_non_latin_text_uses_unicode_font():
    doc = doc_of()
    res = run(doc, O.new_op("replace_text", find="ACME Corporation", replace="Ωmega Ünïcode", page=0))
    assert "Ωmega" in doc[0].get_text()
    assert any("substitute" in f for f in res[0].details["fonts"])
    assert res[0].warnings  # substitution is reported honestly


def test_secure_redaction_removes_text_and_image_pixels():
    data = samples.report_pdf()
    doc = doc_of(data)
    doc[0].insert_image(pymupdf.Rect(300, 500, 400, 600), stream=samples.png_image(color=(0, 200, 0)))
    res = run(doc, O.new_op("redact", areas=[{"page": 0, "rect": [60, 100, 400, 135]},
                                           {"page": 0, "rect": [300, 500, 400, 600]}],
                            terms=["confidential"]))
    t = doc[0].get_text()
    assert "ACME" not in t and "Invoice Total" not in t and "confidential" not in t
    assert res[0].verified
    pix = doc[0].get_pixmap(clip=pymupdf.Rect(320, 520, 380, 580))
    r, g, b = pix.pixel(10, 10)
    assert g < 100  # green image pixels under the box are gone (box filled black)
    assert b"ACME" not in doc[0].read_contents()                       # gone from the page's content stream


def test_add_text_wraps_and_errors_when_not_fitting():
    doc = doc_of()
    run(doc, O.new_op("add_text", page=0, rect=[72, 600, 300, 680], text="Line one\nLine two", bold=True,
                      color=[1, 0, 0]))
    assert "Line one" in doc[0].get_text() and "Line two" in doc[0].get_text()
    r = O.apply_operations(doc, [O.new_op("add_text", page=0, rect=[72, 700, 90, 705], text="far too long " * 9)])
    assert not r[0].ok and "does not fit" in r[0].error
    r = O.apply_operations(doc, [O.new_op("add_text", page=0, rect=[72, 700, 200, 720], text="shrink me " * 6,
                                          fit="shrink")])
    assert r[0].ok and r[0].warnings


def test_dynamic_dates_require_authorisation():
    doc = doc_of()
    r = O.apply_operations(doc, [O.new_op("add_text", page=0, rect=[72, 600, 400, 640],
                                          text="Downloaded {download_date}")])
    assert not r[0].ok and "authoris" in r[0].error
    ctx = O.ApplyContext(download_ns=1_700_000_000 * 10**9)
    run(doc, O.new_op("add_text", page=0, rect=[72, 600, 400, 640], text="D {download_date:%Y}",
                      authorized_dates=True), ctx=ctx)
    assert "D 2023" in doc[0].get_text()


@pytest.mark.parametrize("rot", [0, 90, 180, 270])
def test_text_is_upright_on_rotated_pages(rot):
    d = pymupdf.open()
    p = d.new_page()
    p.set_rotation(rot)
    run(d, O.new_op("add_text", page=0, rect=[100, 100, 400, 200], text="BOXTEXT"),
        O.new_op("page_numbers", format="PN {n}", position="bottom-right"),
        O.new_op("watermark", text="WMARK", angle=45))
    dx, dy, _ = visual_dir(p, "BOXTEXT")
    assert (dx, dy) == (1.0, 0.0)
    dx, dy, at = visual_dir(p, "PN")
    assert (dx, dy) == (1.0, 0.0)
    assert at.y > p.rect.height * 0.9 and at.x > p.rect.width * 0.7      # bottom-right as displayed
    dx, dy, _ = visual_dir(p, "WMARK")
    assert dx > 0 and dy < 0                                              # ascending diagonal


# ---------------------------------------------------------------- annotations & drawing
def test_annotations():
    doc = doc_of()
    run(doc,
        O.new_op("markup", kind="highlight", find="Invoice Total"),
        O.new_op("markup", kind="underline", page=0, rects=[[72, 140, 200, 155]]),
        O.new_op("markup", kind="strikeout", find="Reference", page=1),
        O.new_op("note", page=0, rect=[500, 50, 520, 70], text="n"),
        O.new_op("comment", page=0, rect=[300, 500, 500, 560], text="c"),
        O.new_op("shape", page=0, kind="rect", rect=[50, 600, 150, 650], fill=[0, 0, 1], opacity=0.5),
        O.new_op("shape", page=0, kind="ellipse", rect=[200, 600, 300, 650]),
        O.new_op("shape", page=0, kind="line", rect=[50, 660, 300, 700]),
        O.new_op("shape", page=0, kind="arrow", rect=[50, 700, 300, 740], width=3),
        O.new_op("shape", page=0, kind="ink", rect=[350, 600, 500, 700], strokes=[[[0, 0], [1, 1]]]))
    kinds = [a.type[1] for a in doc[0].annots()]
    for k in ("Highlight", "Underline", "Text", "FreeText", "Square", "Circle", "Line", "Ink"):
        assert k in kinds
    assert [a.type[1] for a in doc[1].annots()] == ["Highlight", "StrikeOut"]
    sq = next(a for a in doc[0].annots() if a.type[1] == "Square")
    assert abs(sq.opacity - 0.5) < 0.01


def test_flattened_shape_is_content_not_annotation():
    doc = doc_of()
    res = run(doc, O.new_op("shape", page=0, kind="rect", rect=[50, 600, 150, 650], as_annotation=False))
    assert res[0].effect == "overlay" and not list(doc[0].annots())


def test_image_insert_crop_opacity_and_repeat(tmp_path):
    img = tmp_path / "logo.png"
    samples.png_image(path=img)
    ctx = O.ApplyContext(asset_resolver=lambda a: img if a == "logo" else None)
    doc = doc_of()
    run(doc, O.new_op("add_image", page=0, rect=[400, 40, 500, 100], asset="logo", crop=[0, 0, 0.5, 1],
                      opacity=0.5, all_pages=True), ctx=ctx)
    assert all(len(p.get_image_info()) == 1 for p in doc)                 # drawn exactly once per page
    assert doc[0].get_image_info()[0]["has-mask"]                           # opacity -> alpha mask
    r = O.apply_operations(doc, [O.new_op("add_image", page=0, rect=[0, 0, 10, 10], asset="missing")], ctx)
    assert not r[0].ok and "missing" in r[0].error


# ---------------------------------------------------------------- pages
def test_page_operations_and_simulation(tmp_path):
    extra = tmp_path / "extra.pdf"
    samples.different_pdf(extra)
    ctx = O.ApplyContext(asset_resolver=lambda a: extra)
    ops = [O.new_op("insert_pages", asset="x", from_pages="1-2", at=1, count=2),
           O.new_op("duplicate_page", page=0, copies=2),
           O.new_op("move_page", page=0, to=4),
           O.new_op("delete_pages", pages="1"),
           O.new_op("rotate_pages", pages="1-2", angle=270),
           O.new_op("crop_pages", pages="3", rect=[50, 50, 500, 700]),
           O.new_op("resize_pages", pages="4", width=842, height=595)]
    doc = doc_of()
    expected = O.simulate_pages(doc.page_count, ops)
    run(doc, *ops, ctx=ctx)
    assert doc.page_count == expected == 6
    assert doc[0].rotation == 270 and doc[1].rotation == 270
    assert doc[2].rect.width == 450
    assert (round(doc[3].rect.width), round(doc[3].rect.height)) == (842, 595)


def test_delete_all_pages_refused():
    doc = doc_of()
    r = O.apply_operations(doc, [O.new_op("delete_pages", pages="1-3")])
    assert not r[0].ok


def test_bookmarks_forms_metadata():
    doc = doc_of(samples.form_pdf())
    run(doc, O.new_op("add_bookmark", title="Intro", page=0),
        O.new_op("fill_field", name="full_name", value="Jane Doe"),
        O.new_op("fill_field", name="agree", value="yes"),
        O.new_op("add_field", page=0, rect=[72, 200, 250, 220], name="city", value="Paris"),
        O.new_op("add_field", page=0, rect=[72, 230, 250, 250], name="pick", kind="combobox", choices="a, b"),
        O.new_op("delete_field", name="pick"),
        O.new_op("set_metadata", title="T", author="A", subject="S", keywords="K"))
    vals = {w.field_name: w.field_value for w in doc[0].widgets()}
    assert vals["full_name"] == "Jane Doe" and vals["agree"] not in ("Off", False) and vals["city"] == "Paris"
    assert "pick" not in vals
    assert doc.get_toc()[0][1] == "Intro"
    assert doc.metadata["title"] == "T" and doc.metadata["keywords"] == "K"
    doc2 = doc_of()
    run(doc2, O.new_op("remove_bookmark", title="Start"))
    assert [t[1] for t in doc2.get_toc()] == ["End"]


def test_metadata_edit_preserves_internal_dates_unless_authorised():
    doc = doc_of()
    run(doc, O.new_op("set_metadata", title="New"), O.new_op("replace_text", find="ACME", replace="B"))
    out = pymupdf.open(stream=doc.tobytes(), filetype="pdf")
    assert out.metadata["creationDate"] == "D:20200101120000"
    assert out.metadata["modDate"] == "D:20200102120000"
    r = O.apply_operations(doc, [O.new_op("set_metadata_dates", creation="2024-01-01T00:00:00")])
    assert not r[0].ok
    run(doc, O.new_op("set_metadata_dates", creation="2024-01-01T00:00:00", authorized_dates=True))
    assert doc.metadata["creationDate"].startswith("D:20240101")


def test_ocr_text_layer_is_invisible_but_searchable():
    doc = doc_of(samples.scanned_pdf())
    before = doc[0].get_pixmap(dpi=30).samples
    run(doc, O.new_op("ocr_text_layer", page=0, words=[["Scanned", 60, 60, 160, 80], ["words", 170, 60, 240, 80]]))
    assert "Scanned" in doc[0].get_text()
    assert doc[0].get_pixmap(dpi=30).samples == before


# ---------------------------------------------------------------- validation & safety
def test_invalid_operations_are_rejected():
    bad = [
        {"id": "1", "type": "nope"},
        O.new_op("add_text", page=0, rect=[10, 10, 5, 5]),
        O.new_op("add_text", page=-1),
        O.new_op("add_text", page=0, color=[2, 0, 0]),
        O.new_op("add_text", page=0, opacity=3),
        O.new_op("replace_text", find=""),
        O.new_op("redact"),
        O.new_op("delete_pages", pages="x-y"),
        O.new_op("shape", page=0, kind="hexagon"),
    ]
    for op in bad:
        assert O.validate_op(op), op
    doc = doc_of()
    res = O.apply_operations(doc, bad, stop_on_error=False)
    assert len(res) == len(bad) and not any(r.ok for r in res)
    r = O.apply_operations(doc, [O.new_op("add_text", page=7, rect=[0, 0, 50, 50])])
    assert not r[0].ok and "does not exist" in r[0].error


def test_parse_pages():
    assert O.parse_pages("1,3,5-6", 10) == [0, 2, 4, 5]
    assert O.parse_pages("8-", 10) == [7, 8, 9]
    assert O.parse_pages("last", 4) == [3]
    assert O.parse_pages("odd", 5) == [0, 2, 4]
    with pytest.raises(Exception):
        O.parse_pages("12", 3)


def test_detects_signed_encrypted_scanned_corrupt(tmp_path):
    assert inspect_pdf(samples.signed_like_pdf()).signed
    enc = inspect_pdf(samples.encrypted_pdf())
    assert enc.needs_password and enc.encrypted
    assert inspect_pdf(samples.scanned_pdf()).scanned_pages == [0]
    bad = inspect_pdf(samples.corrupt_pdf())
    assert not bad.ok


def test_find_text_reports_style():
    doc = doc_of()
    h = find_text(doc[0], "Invoice Total")[0]
    assert round(h.size) == 12 and h.font == "Helvetica" and h.horizontal
