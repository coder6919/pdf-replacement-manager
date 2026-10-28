# PDF Replacement Manager

A Windows desktop application that remembers how you want specific PDFs modified. When you download a matching
PDF in Chrome, it automatically produces your modified version — with the same filename, in the place you chose,
with filesystem timestamps that reflect **the new download** — and keeps your saved modifications for every future
download.

Everything runs locally. No cloud account, no paid AI, no website passwords or cookies.

---

## Install

**Packaged build (recommended)**

1. Extract `PDFReplacementManager-1.0.0-win64.zip`.
2. Right-click `install.ps1` → *Run with PowerShell* (or `powershell -ExecutionPolicy Bypass -File install.ps1`).
   It installs per user into `%LOCALAPPDATA%\Programs\PDFReplacementManager` and adds a Start-menu entry.
   No administrator rights are needed.
3. Start **PDF Replacement Manager** from the Start menu. It lives in the system tray while monitoring.
4. Optional: *Settings & backup* → *Start with Windows*.

You can also run `PDFReplacementManager.exe` directly from the extracted folder without installing.

**From source**

```bash
py -3.12 -m venv .venv
```
```bash
.venv\Scripts\python -m pip install -r requirements.txt -r requirements-dev.txt
```
```bash
.venv\Scripts\python -m prm
```

## Quick start

1. **Create a profile from a sample** — *PDF editor* → *Open PDF…* → pick a copy of the PDF you download.
2. **Make your changes** (see *Editing* below). Each change becomes a saved, replayable *edit*.
3. **Save** — *Save as new profile…*. The dialog pre-fills the filename (`Report.pdf`) and takes a privacy-preserving
   content fingerprint of the sample (hashes only, no text).
   Or, if you edited the PDF in another program: *Profiles* → *New profile…* → *Import edited master PDF…*.
4. **Choose the method** on the profile's *General* tab:
   * **Apply saved edits to each new download** — your edits are re-applied to every fresh download (new numbers,
     new pages are kept).
   * **Replace with my saved master** — every matching download is replaced by a fresh copy of your edited master.
5. **Choose the output** (*Output* tab): replace in place (default; the original is backed up first), a separate
   folder, or a profile folder; optionally open the result in your PDF viewer or Chrome.
6. **Choose the timestamp policy** (*Timestamps* tab, default **A**).
7. Tick **Process matching downloads automatically**, confirm, and download the PDF in Chrome as usual.

Try it safely first: the *Safety* tab has **Dry run** (generate + validate, never publish), and
*Dashboard* → *Process a PDF now (one time)…* runs a single file through the same pipeline.

## Timestamp policies

| | Creation time | Modified time |
|---|---|---|
| **A — download** (default) | when the output was generated | the **new download's** modified time |
| **B — processing** | generation time | generation time |
| **C — preserve** | the download's creation time | the download's modified time |
| **D — custom** | any of: download modified/created, detected, processing, fixed | same choices |

The master's or profile's own dates are never used. Values are applied *after* the file is published (so neither
copying nor NTFS "tunnelling" can bring back an older date), read back, and verified; the job history shows them in
local time and UTC. The system clock is never changed.

**Three different kinds of dates:** filesystem timestamps (above); the PDF's *internal* CreationDate/ModDate (kept
unchanged unless you add the explicit, authorised *Internal PDF dates* edit); and dates *printed on the page*
(changed only by a text edit you make — dynamic date tokens additionally require ticking “I am authorised…”).

## Editing

Tools: **Select** (move / resize with the corner handle / Del), **Edit text** (click a word or drag across text on
one line → replace or delete), **Text**, **Highlight / Underline / Strike**, **Note**, **Comment**, **Rectangle /
Ellipse / Line / Arrow / Freehand**, **Image**, **Redact**, **Form field**, **Crop**.
Menus: *Pages* (insert from PDF, duplicate, rotate, move, delete, page size, split/extract), *Document* (page
numbers, header/footer, text or image watermark, properties, bookmarks, fill forms, redact search terms, OCR,
internal dates). Also: find, replace-all, undo/redo, zoom, thumbnails, **Compare original** side by side and **Show
differences** in red, a local plain-language **Assistant** that proposes edits for you to confirm, autosave and
recovery (*Open profile > Recover autosaved session*), *Export PDF…*, *Save as master…*.

Every edit shows what it really does:

* **Genuine edit / Genuine removal** — the old glyphs are removed from the page content (never hidden under a box).
* **Overlay** — new content drawn on top; what is underneath is unchanged.
* **Annotation** — a separate object other PDF editors can remove.

See [docs/EDITING_CAPABILITIES.md](docs/EDITING_CAPABILITIES.md) for the tested capability matrix and limitations
(scanned pages, fonts, signatures, encryption).

## How matching works

A download matches a profile when its **filename** (exact, case-insensitive, also Chrome's `Report (1).pdf`) or
**pattern** or **SHA-256** matches — and, if you set them, its **website** (read locally from the
`Zone.Identifier` information Chrome stores with each download). Because a name alone does not guarantee the same
document, a content check compares a hashed fingerprint with the reference; if the new file is substantially
different, the job waits in *Needs your attention* for you to approve, choose another profile, or ignore it.

## Safety guarantees

* The original download is copied to the backup store and hash-verified **before** anything else happens.
* The output is built in a staging folder, validated by two independent PDF parsers (MuPDF and qpdf), page count
  and every edit's self-check verified — only then is it published by an atomic rename.
* A crash, forced shutdown or reboot mid-job is recovered on next start (finish or retry; temp files cleaned).
* Files locked by a PDF viewer are retried automatically; full disks are detected before writing.
* Our own outputs are recognised and never re-processed (no loops). Unrelated files are never overwritten.
* Password-protected PDFs are never processed; signed PDFs are not edited unless you explicitly allow it.
* Mark-of-the-Web is kept on generated files.
* Restore any original (*Queue & history* → *Restore original…*) and any profile version (profile → *Versions*).

## Documentation

* [Architecture & design](docs/ARCHITECTURE.md)
* [Editing capability matrix & unsupported features](docs/EDITING_CAPABILITIES.md)
* [Chrome integration & limitations](docs/CHROME_INTEGRATION.md)
* [Backup & recovery guide](docs/BACKUP_AND_RECOVERY.md)
* [Troubleshooting](docs/TROUBLESHOOTING.md)
* [Third-party licenses](LICENSES.md)

## Build & test

```bash
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

This creates the virtual environment, installs pinned dependencies, runs the test suite, builds
`dist\PDFReplacementManager\` with PyInstaller, **runs the packaged executable's `--self-test` and GUI
`--smoke-test`**, and zips the result.

Tests only use temporary folders and generated PDFs:

```bash
.venv\Scripts\python -m pytest
```

The real-Chrome end-to-end test (headless Chrome, throw-away profile, local web server) is opt-in:

```bash
set PRM_CHROME_E2E=1 && .venv\Scripts\python -m pytest tests\test_chrome_e2e.py
```

Command-line options: `--minimized`, `--headless` (monitor without a window), `--watch DIR`, `--data-dir DIR`,
`--open file.pdf`, `--self-test`, `--smoke-test`, `--report FILE`.

## Uninstall

Run `uninstall.ps1` from the install folder. Profiles, masters, backups and history are kept in
`%LOCALAPPDATA%\PDFReplacementManager` unless you pass `-RemoveData`. Make a backup first if you may reinstall.

## Data location

`%LOCALAPPDATA%\PDFReplacementManager\` — `prm.sqlite3` (profiles, versions, edits, jobs, history, settings),
`masters\`, `assets\`, `originals\` (backups of downloads, kept 90 days by default), `sessions\` (editor autosave),
`staging\`, `logs\` (JSON lines; no document text), `outputs\`.
