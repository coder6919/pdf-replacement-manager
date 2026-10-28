# Chrome integration

## Primary workflow: watching the download folder (no extension needed)

Chrome writes a download to `Name.pdf.crdownload` (sometimes `Unconfirmed 1234.crdownload`) and renames it to
`Name.pdf` when complete. The application:

1. receives the rename/create event from Windows (plus a rescan every 30 s and a scan at startup for files that
   arrived while it was closed);
2. ignores `.crdownload`, `.tmp`, `.part` names and waits while a sibling `.crdownload` exists;
3. waits until size and modification time stop changing and the file can be opened without anyone else writing;
4. records the download's own timestamps and Chrome's **Zone.Identifier** (Mark-of-the-Web) — which contains the
   page and file URL (`ReferrerUrl`, `HostUrl`). Query strings, user names and ports are dropped before storing;
5. queues the job (persistent, de-duplicated) and processes it.

This was verified with a real headless Chrome download (`tests/test_chrome_e2e.py`): detection, website matching
via Zone.Identifier, timestamps, the second download arriving as `Report (1).pdf`, and no re-processing of outputs.

Settings Chrome needs: downloads should go to the watched folder without asking (Chrome default). If Chrome is set to
*Ask where to save each file*, add each folder you save into under *Settings & backup → Watched folders*.

If Chrome opens PDFs in its viewer instead of downloading them, the file is not saved to disk and nothing can be
processed. Either use the viewer's download button, or set Chrome → Settings → Privacy and security → Site settings
→ Additional content settings → PDF documents → *Download PDFs*.

## What is (and is not) possible

* Changing a local file does **not** change what the website serves. The website still has the original.
* A Chrome tab that already shows the website's PDF is **not** redirected. The application shows the result by
  opening the generated local file (default viewer, or a new Chrome tab with a `file:///` URL — option per profile).
* Signed/temporary download URLs often expire; matching never re-downloads anything.
* Some sites build the PDF in the page and save it as a `blob:` download. Zone.Identifier then contains no useful
  website; use filename/hash rules, or the optional extension below.
* Authenticated sites work normally: you log in as usual; the application only sees the finished file.
* Collision names: when `Report.pdf` already exists, Chrome saves `Report (1).pdf`. Profiles match it by default;
  the output either replaces `Report (1).pdf` in place or — with *Duplicate names → canonical* — overwrites our own
  previous `Report.pdf` and retires the duplicate (backed up first). The job history explains which happened.

## Optional companion extension (`chrome_extension/`)

Justification: only useful for `blob:`/data downloads (no source URL on disk) and for a status popup in Chrome.

* Permissions: `downloads` (to see completed download metadata), `storage`, and `http://127.0.0.1/*` (to reach the
  local app). No `cookies`, no content scripts, no `webRequest`, no access to page content.
* Only downloads from websites you list in the extension's options are reported, and only `{file name, URL without
  query string, referrer without query string}`.
* The app's bridge is **disabled by default**, binds to `127.0.0.1` only, requires a random pairing token, rejects
  requests from web pages (any `http(s)` Origin), sends no CORS headers, and accepts only small JSON hints.

Install: *Settings & backup* → enable the bridge → *Copy* the token. In Chrome open `chrome://extensions`, enable
*Developer mode*, *Load unpacked* → select the `chrome_extension` folder (shipped next to the exe). Open the
extension's options, paste the token, list the allowed websites, *Save*.

No request interception or page manipulation is used.
