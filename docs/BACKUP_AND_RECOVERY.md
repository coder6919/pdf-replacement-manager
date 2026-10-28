# Backup & recovery guide

## What is stored where

`%LOCALAPPDATA%\PDFReplacementManager\`

| Item | Location | Notes |
|---|---|---|
| Profiles, every profile version, edits, fingerprints, jobs, history, settings | `prm.sqlite3` | SQLite, WAL journal, `synchronous=FULL` |
| Saved masters | `masters\<sha256>.pdf` | SHA-256 recorded; verified before every use |
| Images / PDFs used by edits, design samples | `assets\`, `masters\` | content-addressed |
| Backups of original downloads | `originals\job-<id>\<name>.pdf` | hash-verified copy made **before** processing; kept 90 days (configurable) |
| Editor autosave | `sessions\` | recover via *PDF editor → Open profile → Recover autosaved session* |
| Logs | `logs\prm.log.jsonl` | no document text |

## Making a backup

*Settings & backup → Create backup…* writes a ZIP with a consistent snapshot of the database, all masters and
assets, and (optionally) the original-download backups, plus a manifest of SHA-256 checksums. The archive is
verified after writing. **The ZIP is not encrypted** — keep it somewhere private (it can contain your documents).

## Restoring a backup

*Settings & backup → Restore backup…*

1. Every member is checked against the manifest (checksums, no absolute paths / `..` / unexpected folders).
2. Monitoring stops; the current database is copied to `backups\pre-restore-<time>.sqlite3`.
3. Files are merged into the data folder (nothing is deleted; a different file at the same path is kept aside as
   `*.pre-restore-<time>`), then the database is replaced and monitoring restarts.

To undo a restore, restore again from a backup made before, or replace `prm.sqlite3` with the pre-restore copy
while the app is closed.

## Restoring an original download

*Queue & history* → select the job → *Restore original…* → original location (an unrelated file there is never
overwritten; a numbered name is used) or a location you choose. The restored file gets the download's original
timestamps back.

## Restoring an earlier profile version

Open the profile → *Versions* → select → *Restore selected version*. This creates a **new** version identical to
the old one; history is never rewritten.

## Interrupted processing

Each job journals its step. On the next start (after a crash, forced shutdown or Windows restart) the application:

* removes leftover `~prm-*.tmp` files next to outputs;
* if the output had already been published and matches the validated result, re-applies and verifies the timestamps
  and marks the job done;
* otherwise re-queues the job (the original is still in place, or restorable from its verified backup);
* stops after three interrupted attempts and reports the job as failed.

Downloads that arrive while the app is not running are picked up at the next start.

## Retrying

Failed jobs can be retried from *Queue & history*. Temporary problems (file locked by a PDF viewer, low disk space)
are retried automatically every minute, up to three attempts.

## Moving to a new PC

Create a backup with originals, install the app on the new PC, restore the backup, then check
*Settings → Watched folders* (the Downloads path may differ).
