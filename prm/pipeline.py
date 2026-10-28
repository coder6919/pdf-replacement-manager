"""Transactional processing of one download job.

Ordering guarantees (see docs/ARCHITECTURE.md §4):
  backup verified  ->  output staged  ->  output validated  ->  published atomically  ->  timestamps set & verified
The original is never removed or overwritten before the output has passed validation, and every step is
journalled in jobs.step so recover() can finish or retry after a crash.
"""
from __future__ import annotations

import json
import shutil
import time
from datetime import datetime
from pathlib import Path

import pymupdf

from .errors import (CorruptPdfError, EncryptedPdfError, FileLockedError, IntegrityError, NeedsReview, PRMError,
                     SignedPdfError, ValidationError)
from .fsutil import (copy_verified, ensure_free_space, normalize_collision_name, open_in_chrome,
                     open_with_default_app, publish_atomic, sanitize_filename, sha256_file, unique_path)
from .logsetup import get_logger
from .matching import match
from .operations import ApplyContext, apply_operations, simulate_pages
from .pdfinfo import content_fingerprint, inspect_doc, open_pdf
from .repository import Profile, Repository
from .timestamps import TimestampContext, TimestampPolicy, apply_and_verify, resolve_policy

log = get_logger("pipeline")
TERMINAL = ("done", "failed", "needs_review", "skipped", "ignored", "dry_run", "superseded")
MAX_ATTEMPTS = 3


class Processor:
    def __init__(self, repo: Repository, *, notifier=None, opener=None, chrome_opener=None):
        self.repo = repo
        self.paths = repo.paths
        self.notifier = notifier or (lambda job, kind, msg: None)
        self.opener = opener or open_with_default_app
        self.chrome_opener = chrome_opener or open_in_chrome
        self.publishing: set[str] = set()   # paths being written right now (watcher ignores them)

    # ------------------------------------------------------------------ helpers
    def _step(self, job_id, step, **extra):
        self.repo.update_job(job_id, step=step, **extra)

    def _event(self, job_id, level, msg, data=None):
        self.repo.event(job_id, level, msg, data)
        getattr(log, "warning" if level == "warn" else level if level in ("info", "error") else "info")(
            msg, extra={"job_id": job_id, **({"step": data.get("step")} if data and "step" in data else {})})

    def _fail(self, job_id, state, code, message, retryable=False):
        self.repo.update_job(job_id, state=state, error_code=code, error=message, finished_ns=time.time_ns())
        self._event(job_id, "error" if state == "failed" else "warn", message, {"code": code,
                                                                                "retryable": retryable})
        job = self.repo.job(job_id)
        self.notifier(job, state, message)

    # ------------------------------------------------------------------ main entry
    def process(self, job_id: int) -> str:
        job = self.repo.job(job_id)
        if not job or job["state"] != "queued":
            return job["state"] if job else "missing"
        t0 = time.perf_counter()
        self.repo.update_job(job_id, state="processing", attempts=job["attempts"] + 1, started_ns=time.time_ns(),
                             error=None, error_code=None)
        try:
            state = self._run(job_id)
        except NeedsReview as e:
            self._fail(job_id, "needs_review", e.code, e.user_message)
            state = "needs_review"
        except PRMError as e:
            self._fail(job_id, "failed", e.code, e.user_message, e.retryable)
            state = "failed"
        except Exception as e:  # unexpected: keep original, report generic failure
            log.exception("unexpected failure", extra={"job_id": job_id})
            self._fail(job_id, "failed", "internal", f"Unexpected error ({type(e).__name__}). The original file "
                                                      f"was not changed. See Diagnostics for details.")
            state = "failed"
        log.info("job finished", extra={"job_id": job_id, "state": state,
                                        "duration_ms": int((time.perf_counter() - t0) * 1000)})
        return state

    def _run(self, job_id: int) -> str:
        job = self.repo.job(job_id)
        src = Path(job["source_path"])
        if not src.is_file():
            raise PRMError("The downloaded file no longer exists (moved or deleted before processing).")
        size = src.stat().st_size
        if size == 0:
            raise CorruptPdfError("The downloaded file is empty.")

        # 1. identity & loop prevention ------------------------------------------------------------------
        sha = job["sha256"] or sha256_file(src)
        if job["sha256"] and sha256_file(src) != job["sha256"]:
            self.repo.update_job(job_id, state="superseded", finished_ns=time.time_ns(),
                                 error="File changed after it was detected; a newer job handles it.")
            return "superseded"
        self.repo.update_job(job_id, sha256=sha, size=size)
        if self.repo.is_our_output(str(src), sha):
            self.repo.update_job(job_id, state="ignored", finished_ns=time.time_ns(),
                                 match_reason="This file is an output this application published.")
            return "ignored"

        # 2. inspect ----------------------------------------------------------------------------------------
        try:
            doc = open_pdf(src)
        except Exception:
            raise CorruptPdfError("The file is not a readable PDF (corrupt or incomplete). It was left untouched.")
        try:
            info = inspect_doc(doc)
            fp = None if info.needs_password else content_fingerprint(doc)
        except Exception:
            raise CorruptPdfError("The PDF is damaged and could not be analysed. It was left untouched.")
        finally:
            doc.close()
        if not info.needs_password and info.page_count == 0:
            raise CorruptPdfError("The file is damaged or incomplete (no readable pages). It was left untouched.")
        if info.needs_password:
            raise EncryptedPdfError("This PDF is password protected. It was left untouched; this application "
                                    "never asks for or stores document passwords.")
        self._event(job_id, "info", "Inspected download", {"pages": info.page_count, "signed": info.signed,
                                                            "encrypted": info.encrypted, "warnings": info.warnings})

        # 3. match ------------------------------------------------------------------------------------------
        profiles = self.repo.list_profiles()
        m = match(profiles, job["file_name"], sha, job["source_url"], fp,
                  forced_profile_id=job["profile_id"] if job["force"] else None, force=bool(job["force"]))
        self.repo.update_job(job_id, match_reason=m.reason, similarity=m.similarity,
                             validation=json.dumps({"considered": m.considered}))
        if m.decision == "none":
            self.repo.update_job(job_id, state="skipped", finished_ns=time.time_ns())
            return "skipped"
        if m.decision == "review":
            if m.profile:
                self.repo.update_job(job_id, profile_id=m.profile.id, profile_version_id=m.profile.version_id)
            raise NeedsReview(m.reason)
        prof: Profile = m.profile
        self.repo.update_job(job_id, profile_id=prof.id, profile_version_id=prof.version_id, mode=prof.mode)
        self._event(job_id, "info", m.reason)
        opts = prof.options
        dry = bool(job["dry_run"] or opts.get("dry_run"))

        if prof.mode == "operations":
            if info.signed and not opts.get("allow_signed"):
                raise SignedPdfError("This PDF is digitally signed; applying edits would invalidate the signature. "
                                     "Not processed. Enable 'Allow editing signed PDFs' on the profile only if you "
                                     "accept that.")
            if info.encrypted and not opts.get("allow_encrypted"):
                raise EncryptedPdfError("This PDF is encrypted (permission restrictions). Not processed. The "
                                        "profile can explicitly allow editing such files.")

        # 4. space ------------------------------------------------------------------------------------------
        ensure_free_space(self.paths.root, size * 3)

        # 5. backup original --------------------------------------------------------------------------------
        if not dry and not job["backup_file_id"]:
            fid = self.repo.store_file(src, "original", job_id=job_id, original_name=job["file_name"],
                                       profile_id=prof.id)
            if self.repo.file_row(fid)["sha256"] != sha:
                raise IntegrityError("Original backup does not match the download (file changed while copying).")
            self._step(job_id, "backed_up", backup_file_id=fid)
            self._event(job_id, "info", "Original backed up", {"step": "backed_up"})

        # 6. stage ------------------------------------------------------------------------------------------
        stage_dir = self.paths.staging / f"job-{job_id}"
        if stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)
        stage_dir.mkdir(parents=True)
        staged = stage_dir / "output.pdf"
        ctx = ApplyContext(asset_resolver=self.repo.asset_resolver, file_name=job["file_name"],
                           download_ns=job["download_modified_ns"], processing_ns=time.time_ns())
        op_results = []
        if prof.mode == "master":
            master = self.repo.file_path(prof.master_file_id, verify=True)   # integrity-checked
            copy_verified(master, staged)
            expected_pages = None
        else:
            doc = open_pdf(src)
            try:
                if doc.is_encrypted:
                    doc.authenticate("")
                expected_pages = simulate_pages(doc.page_count, prof.operations)
                results = apply_operations(doc, prof.operations, ctx)
                op_results = [r.to_dict() for r in results]
                self.repo.update_job(job_id, op_results=op_results)
                failed = [r for r in results if not r.ok]
                if failed:
                    raise PRMError(f"Edit '{failed[0].type}' failed: {failed[0].error} The original is untouched.")
                if opts.get("strict_verification", True) and any(not r.verified for r in results):
                    bad = next(r for r in results if not r.verified)
                    raise PRMError(f"Edit '{bad.type}' could not verify its result ({'; '.join(bad.warnings)}). "
                                   f"Not published (strict verification).")
                doc.save(str(staged), garbage=1, deflate=True)
            finally:
                doc.close()
        out_sha = sha256_file(staged)
        self._step(job_id, "staged", staging_path=str(staged), output_sha256=out_sha)

        # 7. validate ---------------------------------------------------------------------------------------
        validation = validate_output(staged, expected_pages, op_results)
        self.repo.update_job(job_id, validation=validation)
        if not validation["ok"]:
            raise ValidationError("Generated PDF failed validation: " + "; ".join(validation["problems"]) +
                                  " The original is untouched.")
        self._step(job_id, "validated")

        target = self.output_target(prof, job)
        self.repo.update_job(job_id, output_path=str(target))
        if dry:
            self.repo.update_job(job_id, state="dry_run", finished_ns=time.time_ns())
            self._event(job_id, "info", f"Dry run: would publish to {target}", {"step": "dry_run"})
            self.notifier(self.repo.job(job_id), "dry_run", f"Dry run complete for {job['file_name']}")
            return "dry_run"

        # 8. publish ----------------------------------------------------------------------------------------
        ensure_free_space(target.parent, size + Path(staged).stat().st_size)
        if prof.output.get("mode", "replace") == "replace" and target.resolve() == src.resolve():
            if sha256_file(src) != sha:
                self.repo.update_job(job_id, state="superseded", finished_ns=time.time_ns(),
                                     error="Download changed during processing; a newer job handles it.")
                return "superseded"
        self.repo.record_published(job_id, str(target), out_sha)   # before rename: our own event is ignored
        key = str(target.resolve()).lower()
        self.publishing.add(key)
        try:
            self._step(job_id, "publishing")
            publish_atomic(staged, target, out_sha, zone_raw=job["zone_raw"])
        finally:
            self.publishing.discard(key)
        self._step(job_id, "published")
        retired = self._retire_collision_duplicate(prof, job, target, src)

        # 9. timestamps -------------------------------------------------------------------------------------
        ts = self.apply_timestamps(prof, job, target)
        self.repo.update_job(job_id, timestamps=ts)
        if not ts["verified"]:
            raise PRMError("Output published, but its timestamps could not be verified "
                           f"(filesystem {ts['filesystem']}). The original backup is kept.")
        self._step(job_id, "timestamps")

        # 10. post-publish verification ------------------------------------------------------------------
        if sha256_file(target) != out_sha:
            raise IntegrityError("Published file does not match the validated output.")
        post = validate_output(target, expected_pages, [])
        if not post["ok"]:
            raise ValidationError("Published file could not be opened: " + "; ".join(post["problems"]))
        self.repo.update_job(job_id, state="done", step="done", finished_ns=time.time_ns())
        msg = f"Processed '{job['file_name']}' with profile '{prof.name}' → {target}"
        if retired:
            msg += f" (Chrome's duplicate '{job['file_name']}' was retired; backup kept)"
        self._event(job_id, "info", msg, {"step": "done"})
        shutil.rmtree(stage_dir, ignore_errors=True)
        self.notifier(self.repo.job(job_id), "done", msg)
        self._post_open(prof, target)
        return "done"

    # ------------------------------------------------------------------ pieces
    def output_target(self, prof: Profile, job) -> Path:
        src = Path(job["source_path"])
        out = prof.output
        mode = out.get("mode", "replace")
        actual = job["file_name"]
        canonical = normalize_collision_name(actual)
        if mode == "replace":
            if canonical != actual and out.get("collision_policy") == "canonical":
                cand = src.with_name(canonical)
                if not cand.exists() or self.repo.output_owned_sha(str(cand)) & {sha256_file(cand)}:
                    return cand
                self._event(job["id"], "warn", f"Chrome saved this as '{actual}' because '{canonical}' already "
                                               f"exists and is not one of this app's outputs, so '{actual}' is "
                                               f"replaced in place instead.")
            elif canonical != actual:
                self._event(job["id"], "info", f"Chrome saved this download as '{actual}' because "
                                               f"'{canonical}' already existed. The output replaces '{actual}'.")
            return src
        tpl = out.get("filename_template") or "{name}"
        name = tpl.replace("{name}", canonical).replace("{stem}", Path(canonical).stem) \
                  .replace("{profile}", prof.name).replace("{date}", datetime.now().strftime("%Y-%m-%d"))
        name = sanitize_filename(name)
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        if mode == "directory":
            base = Path(out["directory"]).expanduser()
        else:
            base = (Path(out["directory"]).expanduser() if out.get("directory") else self.paths.outputs) / \
                   sanitize_filename(prof.name, "profile")
        target = base / name
        if target.exists() and not self._owned(target):     # never overwrite unrelated files
            cand = target
            for i in range(2, 10000):
                cand = target.with_name(f"{target.stem} ({i}){target.suffix}")
                if not cand.exists() or self._owned(cand):
                    break
            self._event(job["id"], "warn", f"'{target}' exists and was not created by this app; writing "
                                           f"'{cand.name}' instead.")
            target = cand
        return target

    def _owned(self, path: Path) -> bool:
        return sha256_file(path) in self.repo.output_owned_sha(str(path))

    def _retire_collision_duplicate(self, prof, job, target: Path, src: Path) -> bool:
        if target.resolve() == src.resolve() or prof.output.get("mode", "replace") != "replace":
            return False
        # output went to the canonical name; the collision-named download is backed up -> remove duplicate
        if job["backup_file_id"] or self.repo.job(job["id"])["backup_file_id"]:
            try:
                src.unlink()
                return True
            except OSError:
                return False
        return False

    def apply_timestamps(self, prof: Profile, job, target: Path) -> dict:
        policy = TimestampPolicy.from_dict(prof.timestamp_policy)
        ctx = TimestampContext(download_created_ns=job["download_created_ns"],
                               download_modified_ns=job["download_modified_ns"],
                               detected_ns=job["detected_ns"], processing_ns=time.time_ns())
        c, m = resolve_policy(policy, ctx)
        res = apply_and_verify(target, c, m)
        res["policy"] = policy.to_dict()
        res["policy_description"] = policy.describe()
        return res

    def _post_open(self, prof: Profile, target: Path):
        try:
            if prof.output.get("open_after"):
                self.opener(target)
            if prof.output.get("open_in_chrome"):
                self.chrome_opener(target)
        except Exception as e:
            log.warning("could not open output: %s", type(e).__name__)

    # ------------------------------------------------------------------ recovery & user actions
    def recover(self) -> list[int]:
        """Called at startup: finish or re-queue jobs interrupted by a crash, shutdown or reboot."""
        recovered = []
        for job in self.repo.jobs(("processing",)):
            jid = job["id"]
            target = Path(job["output_path"]) if job["output_path"] else None
            if target:
                for tmp in target.parent.glob("~prm-*.tmp"):
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
            if target and target.is_file() and job["output_sha256"] and job["step"] in (
                    "publishing", "published", "timestamps"):
                if sha256_file(target) == job["output_sha256"]:
                    prof = self.repo.get_profile(job["profile_id"], job["profile_version_id"])
                    ts = self.apply_timestamps(prof, job, target)
                    state = "done" if ts["verified"] else "failed"
                    self.repo.update_job(jid, state=state, step="done", timestamps=ts, finished_ns=time.time_ns(),
                                         error=None if ts["verified"] else "Timestamp verification failed.")
                    self.repo.event(jid, "info", "Recovered after interruption: publish had completed; "
                                                 "timestamps re-applied and verified.", {"recovery": "finish"})
                    recovered.append(jid)
                    continue
            src = Path(job["source_path"])
            if job["sha256"] and src.is_file() and sha256_file(src) not in (job["sha256"], job["output_sha256"]):
                self.repo.update_job(jid, state="failed", error="Interrupted, and the file was modified by "
                                                                "something else since. Original backup kept.")
                continue
            if job["attempts"] >= MAX_ATTEMPTS:
                self.repo.update_job(jid, state="failed", error="Processing was interrupted repeatedly; "
                                                                "stopped retrying. Original kept.")
            else:
                self.repo.update_job(jid, state="queued", step="recovered")
                self.repo.event(jid, "warn", "Job was interrupted (application closed or crashed); re-queued.",
                                {"recovery": "requeue"})
            recovered.append(jid)
        # stale staging folders of finished jobs
        for d in self.paths.staging.glob("job-*"):
            try:
                j = self.repo.job(int(d.name[4:]))
            except ValueError:
                continue
            if not j or j["state"] in ("done", "failed", "skipped", "ignored", "superseded"):
                shutil.rmtree(d, ignore_errors=True)
        return recovered

    def retry(self, job_id: int):
        self.repo.update_job(job_id, state="queued", error=None, error_code=None)
        self.repo.event(job_id, "info", "Retry requested")

    def approve(self, job_id: int, profile_id: int, update_reference: bool = False):
        """User decision for a needs_review job: process with the chosen profile despite the guard."""
        job = self.repo.job(job_id)
        if update_reference:
            prof = self.repo.get_profile(profile_id)
            from .pdfinfo import fingerprint_file
            prof.reference_fingerprint = fingerprint_file(job["source_path"])
            self.repo.save_profile(prof, note="Reference fingerprint updated from an approved download",
                                   validate=False)
        self.repo.update_job(job_id, state="queued", profile_id=profile_id, force=1, error=None, error_code=None)
        self.repo.event(job_id, "info", "Approved by user for processing")

    def restore_original(self, job_id: int, destination: Path | None = None) -> Path:
        """Put the backed-up original back (to its original path by default)."""
        job = self.repo.job(job_id)
        if not job["backup_file_id"]:
            raise PRMError("No backup of the original exists for this job.")
        backup = self.repo.file_path(job["backup_file_id"], verify=True)
        dest = Path(destination) if destination else Path(job["source_path"])
        if dest.exists():
            cur = sha256_file(dest)
            if cur != job["output_sha256"] and cur != job["sha256"]:
                dest = unique_path(dest)   # never clobber an unrelated file
        copy_verified(backup, dest)
        from .timestamps import set_times
        set_times(dest, created_ns=job["download_created_ns"], modified_ns=job["download_modified_ns"])
        self.repo.event(job_id, "info", f"Original restored to {dest}")
        return dest


def validate_output(path: Path | str, expected_pages: int | None, op_results: list) -> dict:
    problems, checks = [], {}
    p = Path(path)
    if not p.is_file() or p.stat().st_size == 0:
        return {"ok": False, "problems": ["output file is missing or empty"], "checks": {}}
    checks["size"] = p.stat().st_size
    try:
        doc = pymupdf.open(str(p))
    except Exception as e:
        return {"ok": False, "problems": [f"MuPDF cannot open the output ({type(e).__name__})"], "checks": checks}
    try:
        if doc.needs_pass:
            problems.append("output is password protected")
        checks["pages"] = doc.page_count
        if doc.page_count == 0:
            problems.append("output has no pages")
        if expected_pages is not None and doc.page_count != expected_pages:
            problems.append(f"expected {expected_pages} pages, got {doc.page_count}")
        for i, page in enumerate(doc):
            try:
                page.get_text("text")
                if i == 0:
                    page.get_pixmap(dpi=20)
            except Exception:
                problems.append(f"page {i + 1} cannot be rendered")
                break
        checks["mupdf"] = "ok"
    finally:
        doc.close()
    try:
        import pikepdf
        with pikepdf.open(str(p)) as pdf:
            n = len(pdf.pages)
            if "pages" in checks and n != checks["pages"]:
                problems.append("independent parser (qpdf) sees a different page count")
        checks["qpdf"] = "ok"
    except Exception as e:
        problems.append(f"independent parser (qpdf) rejected the output ({type(e).__name__})")
    bad = [r for r in op_results if not r.get("ok")]
    if bad:
        problems.append(f"{len(bad)} operation(s) failed")
    checks["operations"] = len(op_results)
    return {"ok": not problems, "problems": problems, "checks": checks}
