"""Persistence for profiles (versioned), operations, managed files, fingerprints and jobs."""
from __future__ import annotations

import copy
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .db import Database, now_ns
from .errors import IntegrityError, PRMError
from .fsutil import copy_verified, sanitize_filename, sha256_file
from .operations import validate_op
from .paths import AppPaths
from .timestamps import TimestampPolicy

DEFAULT_RULES = {
    "filenames": [],              # exact names, case-insensitive
    "patterns": [],               # glob patterns, e.g. "Report*.pdf"
    "hashes": [],                 # sha256 of known original downloads
    "hosts": [],                  # e.g. "portal.example.com" (from Zone.Identifier / extension)
    "url_patterns": [],           # glob over the source URL
    "match_collision_names": True,
    "content_check": True,        # compare with the reference fingerprint
    "similarity_threshold": 0.55,
}
DEFAULT_OUTPUT = {
    "mode": "replace",            # replace | directory | profile_dir
    "directory": "",
    "filename_template": "{name}",   # tokens: {name} {stem} {profile} {date}
    "open_after": False,
    "open_in_chrome": False,
    # When Chrome saved 'Report (1).pdf' because 'Report.pdf' exists:
    #   in_place  -> replace 'Report (1).pdf' itself
    #   canonical -> write 'Report.pdf' (only if that file is our previous output) and retire the duplicate
    "collision_policy": "in_place",
}
DEFAULT_OPTIONS = {
    "dry_run": False,
    "allow_signed": False,        # operations mode would invalidate signatures
    "allow_encrypted": False,     # owner-password-only encrypted PDFs
    "strict_verification": True,  # fail when an operation cannot verify its own result
    "notify": True,
}


@dataclass
class Profile:
    id: int | None
    name: str
    enabled: bool = False
    priority: int = 0
    mode: str = "operations"
    rules: dict = field(default_factory=lambda: copy.deepcopy(DEFAULT_RULES))
    output: dict = field(default_factory=lambda: copy.deepcopy(DEFAULT_OUTPUT))
    timestamp_policy: dict = field(default_factory=lambda: TimestampPolicy().to_dict())
    options: dict = field(default_factory=lambda: copy.deepcopy(DEFAULT_OPTIONS))
    operations: list = field(default_factory=list)
    master_file_id: int | None = None
    base_file_id: int | None = None     # the PDF the operations were designed on
    reference_fingerprint: dict | None = None
    version_id: int | None = None
    version_no: int = 0
    created_at: int = 0
    updated_at: int = 0

    def validate(self) -> list[str]:
        errs = []
        if not self.name.strip():
            errs.append("Profile name is required.")
        if self.mode not in ("master", "operations"):
            errs.append("Unknown mode.")
        if self.mode == "master" and not self.master_file_id:
            errs.append("Master-replacement mode needs a saved master PDF.")
        if self.mode == "operations" and not self.operations:
            errs.append("Operations mode needs at least one operation.")
        r = self.rules
        if not (r.get("filenames") or r.get("patterns") or r.get("hashes")):
            errs.append("Add at least one filename, filename pattern or file hash to match downloads.")
        try:
            TimestampPolicy.from_dict(self.timestamp_policy)
        except ValueError as e:
            errs.append(str(e))
        if self.output.get("mode") == "directory" and not self.output.get("directory"):
            errs.append("Choose an output directory.")
        for i, op in enumerate(self.operations):
            for e in validate_op(op):
                errs.append(f"Operation {i + 1}: {e}")
        return errs


class Repository:
    def __init__(self, db: Database, paths: AppPaths):
        self.db, self.paths = db, paths

    # ------------------------------------------------------------ managed files
    def store_file(self, src: Path | str, kind: str, *, profile_id=None, job_id=None,
                   original_name: str | None = None) -> int:
        src = Path(src)
        sha = sha256_file(src)
        folder = {"master": self.paths.masters, "asset": self.paths.assets, "reference": self.paths.masters,
                  "original": self.paths.originals, "session": self.paths.sessions}[kind]
        if kind == "original":
            dst = folder / f"job-{job_id}" / sanitize_filename(original_name or src.name)
        else:
            ext = src.suffix.lower() if src.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg", ".ttf", ".otf",
                                                                 ".bmp", ".gif", ".webp") else ".bin"
            dst = folder / f"{sha}{ext}"
        if not (dst.exists() and sha256_file(dst) == sha):
            copy_verified(src, dst, sha)
        with self.db.tx() as c:
            r = c.execute("SELECT id FROM managed_files WHERE kind=? AND sha256=? AND rel_path=? "
                          "AND IFNULL(job_id,-1)=IFNULL(?,-1)", (kind, sha, self.paths.rel(dst), job_id)).fetchone()
            if r:
                return r[0]
            cur = c.execute("INSERT INTO managed_files(kind,rel_path,sha256,size,original_name,profile_id,job_id,"
                            "created_at) VALUES(?,?,?,?,?,?,?,?)",
                            (kind, self.paths.rel(dst), sha, dst.stat().st_size,
                             sanitize_filename(original_name or src.name), profile_id, job_id, now_ns()))
            return cur.lastrowid

    def file_row(self, file_id: int):
        return self.db.q1("SELECT * FROM managed_files WHERE id=?", (file_id,))

    def file_path(self, file_id: int, verify: bool = True) -> Path:
        r = self.file_row(file_id)
        if not r:
            raise IntegrityError("Managed file record is missing.")
        p = self.paths.resolve(r["rel_path"])
        if not p.is_file():
            raise IntegrityError(f"Managed file is missing on disk: {r['rel_path']}")
        if verify and sha256_file(p) != r["sha256"]:
            raise IntegrityError(f"Managed file failed its integrity check: {r['rel_path']}")
        return p

    def asset_resolver(self, asset_id: str) -> Path | None:
        """Assets are referenced in operations by sha256."""
        r = self.db.q1("SELECT id FROM managed_files WHERE kind='asset' AND sha256=? ORDER BY id DESC", (asset_id,))
        return self.file_path(r[0]) if r else None

    def verify_all_files(self) -> list[dict]:
        out = []
        for r in self.db.q("SELECT * FROM managed_files"):
            try:
                self.file_path(r["id"])
                ok, msg = True, "ok"
            except PRMError as e:
                ok, msg = False, e.user_message
            out.append({"id": r["id"], "kind": r["kind"], "path": r["rel_path"], "ok": ok, "message": msg})
        return out

    # ------------------------------------------------------------ profiles
    def list_profiles(self, include_deleted=False) -> list[Profile]:
        rows = self.db.q("SELECT id FROM profiles WHERE deleted=0 OR ? ORDER BY priority DESC, name",
                         (int(include_deleted),))
        return [self.get_profile(r[0]) for r in rows]

    def get_profile(self, profile_id: int, version_id: int | None = None) -> Profile | None:
        p = self.db.q1("SELECT * FROM profiles WHERE id=?", (profile_id,))
        if not p:
            return None
        vid = version_id or p["current_version_id"]
        v = self.db.q1("SELECT * FROM profile_versions WHERE id=?", (vid,)) if vid else None
        prof = Profile(id=p["id"], name=p["name"], enabled=bool(p["enabled"]), priority=p["priority"],
                       created_at=p["created_at"], updated_at=p["updated_at"])
        if v:
            prof.mode = v["mode"]
            prof.rules = {**DEFAULT_RULES, **json.loads(v["match_rules"])}
            prof.output = {**DEFAULT_OUTPUT, **json.loads(v["output"])}
            prof.timestamp_policy = json.loads(v["timestamp_policy"])
            prof.options = {**DEFAULT_OPTIONS, **json.loads(v["options"])}
            prof.master_file_id = v["master_file_id"]
            prof.base_file_id = v["base_file_id"]
            prof.reference_fingerprint = json.loads(v["reference_fingerprint"]) if v["reference_fingerprint"] else None
            prof.version_id, prof.version_no = v["id"], v["version_no"]
            prof.operations = [json.loads(r["params"]) for r in
                               self.db.q("SELECT params FROM operations WHERE version_id=? ORDER BY seq", (v["id"],))]
        return prof

    def get_profile_by_name(self, name: str) -> Profile | None:
        r = self.db.q1("SELECT id FROM profiles WHERE name=? AND deleted=0", (name,))
        return self.get_profile(r[0]) if r else None

    def save_profile(self, prof: Profile, note: str = "", *, validate: bool = True) -> Profile:
        """Persist as a NEW version (append-only history). Returns the reloaded profile."""
        if validate:
            errs = prof.validate()
            if errs:
                raise PRMError("Profile is not valid:\n• " + "\n• ".join(errs))
        TimestampPolicy.from_dict(prof.timestamp_policy)
        t = now_ns()
        with self.db.tx() as c:
            if prof.id is None:
                cur = c.execute("INSERT INTO profiles(name,enabled,priority,created_at,updated_at) VALUES(?,?,?,?,?)",
                                (prof.name.strip(), int(prof.enabled), prof.priority, t, t))
                pid = cur.lastrowid
            else:
                pid = prof.id
                c.execute("UPDATE profiles SET name=?, enabled=?, priority=?, updated_at=? WHERE id=?",
                          (prof.name.strip(), int(prof.enabled), prof.priority, t, pid))
            vno = (c.execute("SELECT MAX(version_no) FROM profile_versions WHERE profile_id=?", (pid,)).fetchone()[0]
                   or 0) + 1
            cur = c.execute(
                "INSERT INTO profile_versions(profile_id,version_no,created_at,note,mode,match_rules,output,"
                "timestamp_policy,options,master_file_id,base_file_id,reference_fingerprint) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, vno, t, note, prof.mode, json.dumps(prof.rules), json.dumps(prof.output),
                 json.dumps(prof.timestamp_policy), json.dumps(prof.options), prof.master_file_id,
                 prof.base_file_id, json.dumps(prof.reference_fingerprint) if prof.reference_fingerprint else None))
            vid = cur.lastrowid
            for seq, op in enumerate(prof.operations):
                c.execute("INSERT INTO operations(version_id,seq,op_type,params) VALUES(?,?,?,?)",
                          (vid, seq, op["type"], json.dumps(op)))
            c.execute("UPDATE profiles SET current_version_id=? WHERE id=?", (vid, pid))
            for fid in (prof.master_file_id, prof.base_file_id):
                if fid:
                    c.execute("UPDATE managed_files SET profile_id=? WHERE id=? AND profile_id IS NULL", (pid, fid))
        return self.get_profile(pid)

    def set_enabled(self, profile_id: int, enabled: bool):
        self.db.x("UPDATE profiles SET enabled=?, updated_at=? WHERE id=?", (int(enabled), now_ns(), profile_id))

    def delete_profile(self, profile_id: int):
        """Soft delete: history, masters and versions are retained for recovery."""
        self.db.x("UPDATE profiles SET deleted=1, enabled=0, name=name||' (deleted '||?||')', updated_at=? "
                  "WHERE id=?", (profile_id, now_ns(), profile_id))

    def versions(self, profile_id: int) -> list:
        return self.db.q("SELECT v.*, (SELECT COUNT(*) FROM operations o WHERE o.version_id=v.id) AS op_count "
                         "FROM profile_versions v WHERE profile_id=? ORDER BY version_no DESC", (profile_id,))

    def restore_version(self, profile_id: int, version_id: int) -> Profile:
        old = self.get_profile(profile_id, version_id)
        if not old or old.id != profile_id:
            raise PRMError("Version not found for this profile.")
        cur = self.get_profile(profile_id)
        old.enabled, old.name = cur.enabled, cur.name
        return self.save_profile(old, note=f"Restored from version {old.version_no}", validate=False)

    def add_fingerprint(self, profile_id: int, kind: str, value: str, note: str = ""):
        self.db.x("INSERT INTO fingerprints(profile_id,kind,value,note,created_at) VALUES(?,?,?,?,?)",
                  (profile_id, kind, value, note, now_ns()))

    def fingerprints(self, profile_id: int):
        return self.db.q("SELECT * FROM fingerprints WHERE profile_id=? ORDER BY id DESC", (profile_id,))

    # ------------------------------------------------------------ jobs
    def create_job(self, *, dedupe_key, source_path, file_name, size, created_ns, modified_ns, detected_ns,
                   source_url=None, referrer_url=None, zone_raw=None, dry_run=False, force=False,
                   sha256=None, profile_id=None) -> int | None:
        t = now_ns()
        try:
            with self.db.tx() as c:
                cur = c.execute(
                    "INSERT INTO jobs(dedupe_key,state,step,source_path,file_name,size,download_created_ns,"
                    "download_modified_ns,detected_ns,source_url,referrer_url,zone_raw,dry_run,force,created_at,"
                    "updated_at,sha256,profile_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (dedupe_key, "queued", "detected", str(source_path), file_name, size, created_ns, modified_ns,
                     detected_ns, source_url, referrer_url, zone_raw, int(dry_run), int(force), t, t, sha256,
                     profile_id))
                return cur.lastrowid
        except Exception as e:
            if "UNIQUE" in str(e):
                return None
            raise

    def job(self, job_id: int):
        return self.db.q1("SELECT * FROM jobs WHERE id=?", (job_id,))

    def update_job(self, job_id: int, **fields):
        fields["updated_at"] = now_ns()
        for k in ("timestamps", "validation", "op_results"):
            if k in fields and not isinstance(fields[k], (str, type(None))):
                fields[k] = json.dumps(fields[k])
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.x(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    def event(self, job_id: int | None, level: str, message: str, data: dict | None = None):
        self.db.x("INSERT INTO job_events(job_id,ts,level,message,data) VALUES(?,?,?,?,?)",
                  (job_id, now_ns(), level, message, json.dumps(data) if data else None))

    def events(self, job_id: int):
        return self.db.q("SELECT * FROM job_events WHERE job_id=? ORDER BY id", (job_id,))

    def jobs(self, states: tuple | None = None, limit: int = 500):
        if states:
            qs = ",".join("?" * len(states))
            return self.db.q(f"SELECT * FROM jobs WHERE state IN ({qs}) ORDER BY id DESC LIMIT ?", (*states, limit))
        return self.db.q("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))

    def next_queued_job(self):
        return self.db.q1("SELECT * FROM jobs WHERE state='queued' ORDER BY id LIMIT 1")

    def record_published(self, job_id: int, path: str, sha: str):
        self.db.x("INSERT INTO published_outputs(job_id,path,sha256,published_ns) VALUES(?,?,?,?)",
                  (job_id, str(Path(path).resolve()), sha, now_ns()))

    def is_our_output(self, path: str, sha: str | None = None) -> bool:
        p = str(Path(path).resolve())
        if sha is None:
            return bool(self.db.q1("SELECT 1 FROM published_outputs WHERE path=?", (p,)))
        return bool(self.db.q1("SELECT 1 FROM published_outputs WHERE path=? AND sha256=?", (p, sha)))

    def output_owned_sha(self, path: str) -> set[str]:
        return {r[0] for r in self.db.q("SELECT sha256 FROM published_outputs WHERE path=?",
                                         (str(Path(path).resolve()),))}

    def purge_originals(self, keep_days: int) -> int:
        """Retention policy for backed-up originals (never removes ones for unfinished jobs)."""
        cutoff = now_ns() - keep_days * 86400 * 10**9
        rows = self.db.q("SELECT m.* FROM managed_files m JOIN jobs j ON j.backup_file_id=m.id "
                         "WHERE m.kind='original' AND m.created_at<? AND j.state IN ('done','skipped','ignored')",
                         (cutoff,))
        n = 0
        for r in rows:
            p = self.paths.resolve(r["rel_path"])
            try:
                if p.exists():
                    p.unlink()
                if p.parent.exists() and not any(p.parent.iterdir()):
                    shutil.rmtree(p.parent, ignore_errors=True)
                self.db.x("DELETE FROM managed_files WHERE id=?", (r["id"],))
                self.db.x("UPDATE jobs SET backup_file_id=NULL WHERE backup_file_id=?", (r["id"],))
                n += 1
            except OSError:
                pass
        return n
