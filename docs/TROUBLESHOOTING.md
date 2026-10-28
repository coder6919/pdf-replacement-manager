# Troubleshooting

Start with **Logs & diagnostics → “Why was / wasn't a file processed?”**: it checks folder, name, origin, PDF
health, every profile's rules and content similarity, and prints the decision — without changing anything.

| Symptom | Likely cause | What to do |
|---|---|---|
| Nothing happens after a download | Monitoring paused, wrong folder, or profile disabled | Dashboard shows *Running* and the watched folder; enable the profile; check Chrome saves to that folder |
| Job says **No matching profile** | Name differs (e.g. `Report_2024-05.pdf`) | Add a pattern such as `Report_*.pdf` |
| Job says **Needs review — content substantially different** | Same name, different document (or the document changed a lot) | *Approve…* to process anyway (optionally update the reference), or lower *Minimum similarity* |
| **Edit failed: text not found** | The new download no longer contains the text, or it's split across lines/fonts | Open the profile in the editor with the new download as sample; mark the edit *optional* if it may be absent |
| **Could not verify its result** | A replacement or redaction could not be confirmed | Check the result in the editor's Compare view; if acceptable, turn off *Strict verification* for that profile |
| **File locked** | The PDF is open in a viewer that blocks replacement | Close the viewer; it retries automatically every minute (or *Retry*) |
| **Password protected** | Encrypted PDF needing a password | Not supported by design; the download is left untouched |
| **Digitally signed** | Editing would break the signature | Use master mode, or explicitly allow editing signed PDFs on the profile |
| **Not enough disk space** | Below ~50 MB + 3× file size free | Free space; it retries automatically |
| Output has the wrong date in Explorer | Timestamp policy | Check the policy on the profile's *Timestamps* tab and the job's *Output timestamps* (requested vs verified values). Explorer shows local time; history shows local and UTC |
| Replaced text looks slightly different | Original font not reusable (subset lacks glyphs) | The edit reports “substitute font”; choose a shorter replacement or accept the substitute |
| Chrome shows the old PDF | The website's tab is not redirected | Open the generated file (per-profile *Open in viewer/Chrome*) |
| Chrome opens PDFs instead of downloading | Chrome's PDF viewer | Chrome settings → PDF documents → *Download PDFs* |
| App does not start / “already running” | Another instance is in the tray | Use the tray icon; or end `PDFReplacementManager.exe` in Task Manager |
| OCR not available | Tesseract not installed | Install Tesseract (UB Mannheim build), set `TESSDATA_PREFIX`, restart |
| Antivirus warning on the exe | Unsigned PyInstaller build | Build from source (`packaging\build.ps1`) or allow the file; never disable antivirus |

## Logs

`%LOCALAPPDATA%\PDFReplacementManager\logs\prm.log.jsonl` — one JSON object per line with job ID, profile ID,
paths, step, duration, validation and timestamp results, error code and recovery action. Document text,
passwords, cookies and tokens are never logged.

## Resetting

Quit from the tray, then rename `%LOCALAPPDATA%\PDFReplacementManager` (keep it as a backup) and start again.
