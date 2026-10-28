"""Decide which profile (if any) applies to a downloaded PDF, and explain why."""
from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from urllib.parse import urlparse

from .fsutil import normalize_collision_name
from .pdfinfo import similarity
from .repository import Profile


@dataclass
class MatchResult:
    decision: str                     # process | review | none
    profile: Profile | None = None
    reason: str = ""
    similarity: float | None = None
    considered: list = field(default_factory=list)   # [(profile name, reason)] for diagnostics


def _host_ok(host: str, patterns: list[str]) -> bool:
    host = (host or "").lower()
    return any(fnmatch(host, p.lower().strip()) or host == p.lower().strip() for p in patterns if p.strip())


def evaluate(prof: Profile, file_name: str, sha: str | None, source_url: str | None):
    """Return (specificity, reason) if the rules match, else (0, reason why not)."""
    r = prof.rules
    names = {file_name.lower()}
    if r.get("match_collision_names", True):
        names.add(normalize_collision_name(file_name).lower())
    score, why = 0, []
    if sha and sha.lower() in {h.lower() for h in r.get("hashes") or []}:
        score, why = 30, ["exact file hash"]
    elif any(n.strip().lower() in names for n in r.get("filenames") or []):
        score, why = 20, ["exact filename"]
    elif any(fnmatch(n, p.strip().lower()) for p in r.get("patterns") or [] for n in names):
        score, why = 10, ["filename pattern"]
    else:
        return 0, "filename/hash rules do not match"
    if r.get("hosts") or r.get("url_patterns"):
        if not source_url:
            return 0, "profile requires a source website, but the download's source URL is unknown"
        host = urlparse(source_url).hostname or ""
        if r.get("hosts") and not _host_ok(host, r["hosts"]):
            return 0, f"download came from '{host}', which is not in the profile's websites"
        if r.get("url_patterns") and not any(fnmatch(source_url, p) for p in r["url_patterns"]):
            return 0, "source URL does not match the profile's URL patterns"
        score += 5
        why.append("website")
    return score, " + ".join(why)


def match(profiles: list[Profile], file_name: str, sha: str | None, source_url: str | None,
          fingerprint: dict | None, forced_profile_id: int | None = None, force: bool = False) -> MatchResult:
    considered, cands = [], []
    for p in profiles:
        if forced_profile_id is not None:
            if p.id == forced_profile_id:
                cands.append((100, p, "chosen manually"))
            continue
        if not p.enabled:
            considered.append((p.name, "profile disabled"))
            continue
        score, reason = evaluate(p, file_name, sha, source_url)
        considered.append((p.name, reason if score else f"no match: {reason}"))
        if score:
            cands.append((score, p, reason))
    if not cands:
        return MatchResult("none", reason="No enabled profile matches this file.", considered=considered)
    cands.sort(key=lambda c: (c[1].priority, c[0]), reverse=True)
    top = cands[0]
    if len(cands) > 1 and (cands[1][1].priority, cands[1][0]) == (top[1].priority, top[0]):
        return MatchResult("review", reason="More than one profile matches equally: " +
                           ", ".join(c[1].name for c in cands[:3]) + ". Choose one.", considered=considered)
    score, prof, reason = top
    res = MatchResult("process", prof, f"Matched '{prof.name}' by {reason}.", considered=considered)
    if force:
        return res
    exact_hash = score >= 30
    if prof.rules.get("content_check", True) and prof.reference_fingerprint and not exact_hash:
        if fingerprint is None:
            res.decision, res.reason = "review", "Could not compute a content fingerprint for comparison."
            return res
        sim = similarity(fingerprint, prof.reference_fingerprint)
        res.similarity = sim
        thr = float(prof.rules.get("similarity_threshold", 0.55))
        if sim < thr:
            res.decision = "review"
            res.reason = (f"Filename matches '{prof.name}', but the content is substantially different from "
                          f"the expected document (similarity {sim:.0%} < {thr:.0%}). Waiting for your decision.")
        else:
            res.reason += f" Content similarity {sim:.0%}."
    return res
