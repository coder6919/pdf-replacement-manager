# PDF Replacement Manager — Architecture

## 1. Environment findings (Phase 1)

| Item | Finding | Consequence |
|---|---|---|
| OS | Windows 11 (NTFS) | `SetFileTime` gives 100 ns timestamp precision; creation time is settable. |
| Python | 3.12 and 3.14 installed | 3.12 chosen: every dependency (PySide6, PyMuPDF, pikepdf, PyInstaller) ships tested wheels. |
| Chrome | Installed | Primary integration = watching the Chrome download folder. |
| Tesseract | **Not installed** | OCR is optional; the OCR button explains how to enable it. The OCR *text layer* operation works without Tesseract (manual/corrected words). |
| Admin rights | Not needed | Everything runs per user; data lives in `%LOCALAPPDATA%`. |

## 2. Stack

| Concern | Choice | Why |
|---|---|---|
| UI | PySide6 (Qt 6) | Native-feeling widgets, `QGraphicsView` for an interactive page canvas, system tray. LGPL. |
| PDF rendering & editing | **PyMuPDF** (MuPDF 1.28) | Rendering, text extraction with per-glyph geometry, *true* redaction (`apply_redactions` removes glyphs/image pixels), annotations, forms, page ops, TOC, metadata, font extraction. AGPL-3.0 (fine for personal use; see LICENSES). |
| Second-opinion parser | **pikepdf** (qpdf) | Independent structural validation of every generated output. Never used to *save* documents, because saving through pikepdf metadata APIs would rewrite XMP dates. |
| Persistence | SQLite (WAL, foreign keys, `user_version` migrations) | Profiles, versions, operations, fingerprints, jobs, events, published outputs, settings. PDFs are stored as managed files with SHA-256 verification, not as BLOBs. |
| Folder watching | watchdog (ReadDirectoryChangesW) + a periodic rescan fallback | Events can be dropped/duplicated; the rescan and the dedupe key make detection reliable. |
| Packaging | PyInstaller one-folder build + per-user install script | No admin, no installer service, AV-friendlier than one-file. |

Verified PyMuPDF behaviours that shape the design (probe scripts, then unit tests):
* All insertion and extraction coordinates are **unrotated** and **CropBox-relative**; display coordinates = point × `page.rotation_matrix`.
* `apply_redactions()` removes only glyphs under the rectangle, leaving neighbours intact.
* `insert_htmlbox()` returns `-1` when text does not fit (used for overflow errors).

## 3. The three objects

```
 Original download ──(copy + SHA-256 verify)──► originals/<job>/file.pdf   (never the only copy is destroyed)
        │
        ▼ matching (filename / pattern / hash / host / content fingerprint)
 Saved profile (DB) ── current version ──► snapshot: rules, mode, output, timestamp policy, options
        │                                   operations table (ordered JSON ops)
        │                                   master file (masters/<sha>.pdf, hash verified)
        ▼
 Generated output (staging/<job>/output.pdf → validated → published → timestamps set & verified)
```

Profile edits always create a **new profile version**; restoring an old version creates another new version
(history is append-only). Profile `created_at`/`updated_at` are stored separately from any job/output time.

## 4. Processing pipeline (transactional)

```
detect (watchdog / rescan / startup scan)
  → ignore temp names (.crdownload, .tmp, .part), non-PDF, our own outputs (path+SHA in published_outputs)
  → stabilise: size+mtime unchanged across polls, no sibling .crdownload, exclusive-read open succeeds
  → record download timestamps NOW (creation/modified, 100 ns) + Zone.Identifier HostUrl/ReferrerUrl
  → dedupe key = path|size|mtime_ns  (UNIQUE in jobs)  → job row state=queued   (persistent queue)
worker (single consumer, survives restart):
  1 inspect (corrupt / password / encrypted / signed / scanned)
  2 match profile (+ content similarity guard → needs_review)
  3 disk-space check
  4 backup original (copy, fsync, hash verify)                       step=backed_up
  5 build output in staging (master copy or replay operations)        step=staged
  6 validate (exists, non-empty, MuPDF + qpdf parse, page count,
              every page loads/renders, each operation self-verified) step=validated
  7 [dry-run stops here]
  8 publish: copy into temp file in target dir → fsync → hash verify
             → copy Mark-of-the-Web → os.replace (atomic same-volume) step=published
  9 apply timestamp policy with SetFileTime AFTER the rename
    (defeats NTFS tunnelling & copy-preserved master times) → read back & verify
 10 verify published file re-opens and hash matches                   state=done
 11 record events, notify, optional open in viewer / Chrome
```
Every step writes `jobs.step` before acting. On startup, `recover()` inspects interrupted jobs: removes stray
temp files, completes a publish whose target already hashes to the staged output, otherwise re-queues
(≤3 attempts). Originals are never deleted before validation; replacement only happens via atomic rename.

Loop prevention: the published path+SHA is written to `published_outputs` *before* the rename, so the watcher
event for our own write is recognised and ignored.

## 5. Timestamp policy

| Policy | Creation time | Modified time |
|---|---|---|
| A `download` (default) | processing time | download's recorded modified time |
| B `processing` | processing time | processing time |
| C `preserve` | download's creation time | download's modified time |
| D `custom` | any of: download_modified, download_created, detected, processing, fixed ISO | same choices |

Values are stored as integer nanoseconds + ISO-8601 UTC in the job record together with the read-back
verification. Tolerance: exact (100 ns) on NTFS/ReFS, 2 s on FAT/exFAT. The system clock is never changed.
Internal PDF metadata dates are untouched unless a separate, explicitly authorised `set_metadata_dates`
operation exists in the profile.

## 6. Editing model

The editor is **operation-based**: document = base PDF + ordered operation list. Every tool creates or edits an
operation (JSON). The same list is replayed on each new matching download (Mode 2) or rendered once and stored as a
master (Mode 1). Undo/redo = operation-list history. Autosave = session file (base PDF hash + ops) written atomically.

Geometry is stored in *media coordinates* (unrotated, mediabox-relative). Page operations (insert/delete/duplicate/
move) carry a page transform so the editor can map each operation to the page on which it finally appears.

Every operation declares its **effect**: `content-removal` (glyphs genuinely removed), `content-replace`
(removed + new text written), `overlay` (new content drawn on top), `annotation` (separate annotation object),
`structure`, `metadata`. The UI shows this, so a visual overlay is never presented as a real edit or redaction.
See `EDITING_CAPABILITIES.md` for the tested capability matrix.

## 7. Matching

Rules (all optional, combined with AND across categories, OR within a category): exact filenames (case-insensitive,
Chrome collision suffix ` (1)` normalised), glob patterns, SHA-256 allow-list, host / URL glob (from the file's
`Zone.Identifier` stream written by Chrome, or the optional extension hint). A content guard compares a privacy-
preserving fingerprint (page count, page sizes, bottom-k hashed word shingles, 64-bit visual dHash) with the
profile's reference; below the threshold the job goes to **needs_review** instead of being processed.
No document text is stored — only hashes.

## 8. Chrome

Downloads are observed locally (no request interception, no cookies, no credentials). Chrome writes the source URL
into the file's `Zone.Identifier` alternate data stream, which gives host matching without any extension.
An optional MV3 extension (disabled bridge by default) can send `{filename, url}` hints for allow-listed hosts to a
token-protected `127.0.0.1` endpoint and display job status. Local files cannot be injected into an already open
website tab; the app opens the generated file in the default viewer or in Chrome as a new `file:///` tab.

## 9. Security

Local-only processing; no network use except the optional loopback bridge. PDFs are untrusted: opened in-process
without JavaScript execution, filenames sanitised (reserved names, traversal, control chars, length), managed paths
resolved inside the data directory only, backup ZIPs validated against a manifest (zip-slip checked). Logs are JSON
lines with job/profile ids, paths, durations, results — never document text, passwords or cookies.

## 10. Data-loss risks and mitigations

| Risk | Mitigation |
|---|---|
| Original overwritten before output valid | Backup + hash verify first; publish only after validation; atomic rename. |
| Crash mid-publish | Step journal + `recover()`; temp files cleaned; hash comparison decides finish vs retry. |
| Output re-detected → infinite loop | `published_outputs` path+SHA check before processing. |
| Wrong document with same name | Content fingerprint guard → needs_review. |
| Unrelated file overwritten in output dir | Overwrite only files we published (tracked by SHA); otherwise unique name. |
| Master silently altered/corrupted | Master SHA stored; verified before every use. |
| Signatures broken | Signed PDFs are refused in operations mode unless the profile explicitly allows it (warning shown). |
| Disk full | Free-space check before backup, staging and publish. |
