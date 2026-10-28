# Editing capability matrix

Engine: PyMuPDF 2.0 / MuPDF 1.28.5 (editing, rendering, redaction), pikepdf/qpdf (validation only).
Every edit is a replayable operation; the same code runs in the editor preview and on each matching download.
“Tested” names the automated test that exercises the operation on generated PDFs.

Legend: ✅ supported and tested · ◐ partially supported (limits listed) · ✗ not available

## Text and layout

| Capability | Status | Effect | Tested by / notes |
|---|---|---|---|
| Search text, select text | ✅ | — | `test_editor_interactions_end_to_end` (search), `selected_line_text` used by *Edit text* |
| Add text box anywhere (wraps inside box) | ✅ | Overlay | `test_add_text_wraps_and_errors_when_not_fitting` |
| Font (Helvetica/Times/Courier families), size, bold, italic, colour, alignment, opacity, background | ✅ | Overlay | same + `test_editor_interactions_end_to_end` |
| Move / resize / remove added text boxes | ✅ | — | `test_undo_redo_and_incremental_render`, UI test |
| Text overflow handling | ✅ | — | error or explicit “shrink to fit” |
| Upright text on rotated pages | ✅ | — | `test_text_is_upright_on_rotated_pages[0/90/180/270]` |
| **Replace existing text (genuine)** | ◐ | Genuine edit | `test_replace_text_is_genuine_and_keeps_layout` — original glyphs removed from the content stream (verified not present in the page content), replacement written on the same baseline; following glyphs keep their exact positions |
| Delete existing text (genuine) | ✅ | Genuine removal | `test_delete_text_genuine_removal_neighbours_intact` |
| Replacement font fidelity | ◐ | — | Reuses the original embedded font when it contains every needed glyph; otherwise a close standard font (or Arial/Segoe UI for non-Latin text) and the edit reports “substitute font” |
| Imported fonts | ◐ | — | Operation supports `font: "asset:<sha>"` with a TTF/OTF in the asset store; no font picker in the UI yet. Respect font licences |
| Text spanning several lines | ✗ | — | Select/replace one line at a time |
| Vertical / rotated text replacement | ✗ | — | Refused with a clear message |
| Reflowing paragraphs after a longer replacement | ✗ | — | Longer text is shrunk to the original width (≥60 % size) or overlaps with a warning |
| Scanned pages | ◐ | Overlay | No text to edit. Options: add text on top, redact areas, or OCR text layer |
| OCR | ◐ | Overlay (invisible text) | Needs Tesseract installed (not bundled). OCR results are shown for correction before an invisible searchable layer is added (`test_ocr_text_layer_is_invisible_but_searchable`). OCR recognition itself was not tested here (Tesseract not installed) |

## Images and drawing

| Capability | Status | Effect | Notes |
|---|---|---|---|
| Insert image, move, resize, remove | ✅ | Overlay | `test_image_insert_crop_opacity_and_repeat` |
| Crop image, opacity, rotate 90° steps, repeat on every page (logo/image watermark), behind content | ✅ | Overlay | same |
| Rectangle, ellipse, line, arrow, freehand | ✅ | Annotation (default) or Overlay (“flattened”) | `test_annotations`, `test_flattened_shape_is_content_not_annotation` |
| Highlight, underline, strike-through, squiggly (by search or by area) | ✅ | Annotation | `test_annotations` |
| Sticky notes, comment boxes | ✅ | Annotation | `test_annotations` |
| Colour, thickness, opacity | ✅ | — | `test_annotations` (opacity checked) |
| Editing images that already exist in the PDF | ✗ | — | Can be covered (overlay) or removed by redaction |

## Pages and document

| Capability | Status | Notes |
|---|---|---|
| Insert pages from other PDFs (merge) | ✅ | `test_page_operations_and_simulation` |
| Delete, duplicate, rotate, reorder pages | ✅ | same; deleting every page is refused |
| Split / extract pages to a new file | ✅ | `test_export_split_pages` (editor export action) |
| Crop pages | ✅ | Sets the CropBox — content outside is hidden, not removed |
| Page size | ◐ | Changes the MediaBox; content is not scaled |
| Page numbers, headers, footers ({page} {total} {filename}) | ✅ | `test_text_is_upright_on_rotated_pages`, smoke test |
| Text watermark (angle, opacity, colour) | ✅ | same |
| Add / remove bookmarks | ✅ | `test_bookmarks_forms_metadata` (removing a bookmark removes its children) |
| View / fill form fields (text, checkbox, combo, list) | ✅ | same |
| Add / delete form fields | ✅ | same; signature fields cannot be filled or created |
| Title, author, subject, keywords | ✅ | `test_metadata_edit_preserves_internal_dates_unless_authorised` |
| Internal CreationDate / ModDate | ✅ (explicit only) | Separate “Internal PDF dates” edit, refused unless “authorised” is ticked |

## Workflow

| Capability | Status | Notes |
|---|---|---|
| Undo / redo | ✅ | `test_undo_redo_and_incremental_render` |
| Autosave, close, reopen, continue | ✅ | `test_autosave_close_reopen_continue_then_master_and_pipeline` |
| Save to profile (versioned), Save as, Export, Save as master | ✅ | same + UI test |
| Compare original vs result (side by side, pixel difference) | ✅ | UI test |
| Preview before committing | ✅ | the main view is always the final output; dry-run for downloads |
| Warnings before destructive operations | ✅ | delete page, redaction, crop, replace-all |
| Natural-language assistant (local, proposals only) | ✅ | `test_nl_assistant_proposes_but_never_guesses`, UI test |

## Redaction

“Redact” uses MuPDF's redaction engine: text glyphs, image pixels and covered vector graphics inside the area are
**removed from the file**, then the area is filled. After applying, the operation verifies that no text remains
inside each area and that search terms are gone (`test_secure_redaction_removes_text_and_image_pixels` checks
the page content stream and the pixels). A black rectangle drawn with the shape tool is **not** a redaction and is
labelled “Annotation”/“Overlay”. Optionally, document metadata can be scrubbed as part of the redaction.

**Note on a MuPDF behaviour (handled):** when glyphs are removed, MuPDF 1.28 shifts the following glyphs of the same
text object left by the width of the last removed glyph. The application snapshots every glyph position before
redaction and re-places any shifted glyph at its exact original position (original font when possible), then
verifies all surviving glyphs are within 0.3 pt of where they were. If that verification fails, the edit is
reported as unverified and, with strict verification on (default), the output is not published.

## Safety with special PDFs

| PDF kind | Behaviour |
|---|---|
| Password protected | Never opened for editing or processed; the download is left untouched |
| Encrypted with permissions only | Not processed in operations mode unless the profile explicitly allows it (protection is not carried over) |
| Digitally signed | Detected (SigFlags / signature values). Edits would invalidate the signature, so operations mode refuses unless explicitly allowed; master mode simply delivers your master |
| Corrupt / truncated / zero pages | Rejected; original untouched |
| JavaScript | Never executed (MuPDF does not run document JavaScript); reported in diagnostics |
| Links, bookmarks, forms, annotations | Preserved by all operations except where the operation itself changes them (e.g. deleting a page removes its links; redaction removes content in its area) |

## Not supported

* Editing text in Type 3 fonts or fonts without a usable Unicode map (search fails → edit reports “not found”).
* Paragraph reflow, multi-column re-layout, editing tables as tables.
* Converting PDFs to Word or other editable formats.
* Editing existing embedded images, vector drawings or annotations created by other software (they can be removed by redaction or covered).
* Creating or validating digital signatures; preserving signatures across edits (impossible by design of PDF signatures).
* Opening password-protected PDFs (by design: the app never handles passwords).
* XFA (dynamic XML) forms — only AcroForm fields are supported.
* Universal compatibility: unusual or damaged PDFs may fail an edit; the job then fails safely and the original is kept.
