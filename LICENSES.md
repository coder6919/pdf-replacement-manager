# Third-party dependencies and licenses

| Component | Version | Used for | License |
|---|---|---|---|
| PyMuPDF (MuPDF 1.28.5) | 2.0 | Rendering, text extraction, redaction, annotations, forms, page operations | **AGPL-3.0** or Artifex commercial |
| pikepdf (bundles qpdf) | 10.17.0 | Independent structural validation of outputs | MPL-2.0 (qpdf: Apache-2.0) |
| PySide6 / Qt 6 | 6.12.0 | Desktop user interface | LGPL-3.0 (or GPL / commercial) |
| watchdog | 6.0.0 | Folder change notifications (ReadDirectoryChangesW) | Apache-2.0 |
| Pillow | 13.0.0 | Image crop/opacity, fingerprint thumbnails, icon | MIT-CMU (HPND) |
| Python | 3.12 | Runtime | PSF-2.0 |
| PyInstaller (build only) | 6.22.3 | Packaging | GPL-2.0 with bootloader exception (output not GPL-bound) |
| pytest, pytest-qt (tests only) | 9.2.0 / 4.5.0 | Automated tests | MIT |

## What the AGPL means here

PyMuPDF/MuPDF are licensed under the GNU Affero GPL v3. Using this application privately is unrestricted.
If you **distribute** the packaged application to others (or offer it as a network service), you must make the
complete corresponding source code available under the AGPL, or obtain a commercial MuPDF licence from
Artifex. This project's own source is provided in full alongside the build.

PyMuPDF 2.0 declares optional dependencies (`pymupdf4llm`, `pymupdf_layout`, `onnxruntime`, `networkx`,
`tabulate`, `PyYAML`) that pip installs automatically; this application does not use them and the packaged
build excludes them.

Optional, not bundled: **Tesseract OCR** (Apache-2.0) — only if you install it yourself for OCR.
