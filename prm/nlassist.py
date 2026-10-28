"""Local, rule-based natural-language assistant: turns plain requests into PROPOSED operations.

It never applies anything itself — the editor shows every proposal for confirmation, and anything
ambiguous is reported back instead of guessed. No network access, no paid API.
"""
from __future__ import annotations

import re

from .operations import new_op

_Q = r"[\"“”'‘’]"


def _quoted(s: str) -> list[str]:
    return re.findall(rf"{_Q}(.+?){_Q}", s)


def _page_spec(s: str) -> str | None:
    m = re.search(r"\bpages?\s+([\d,\-\s]+|last|all|odd|even)", s, re.I)
    if m:
        return m.group(1).replace(" ", "") or None
    if re.search(r"\b(every|all) pages?\b", s, re.I):
        return "all"
    if re.search(r"\b(first page)\b", s, re.I):
        return "1"
    if re.search(r"\blast page\b", s, re.I):
        return "last"
    return None


def _single_page(spec: str | None, n_pages: int) -> int | None:
    if spec and spec.isdigit():
        p = int(spec) - 1
        return p if 0 <= p < n_pages else None
    if spec == "last":
        return n_pages - 1
    return None


def propose(text: str, n_pages: int = 1) -> tuple[list[dict], list[str]]:
    """Return (proposed operations, notes/questions). Splits on ';' / newlines / ' then '."""
    ops, notes = [], []
    for raw in re.split(r";|\n|\bthen\b", text, flags=re.I):
        s = raw.strip()
        if not s:
            continue
        low = s.lower()
        q = _quoted(s)
        spec = _page_spec(s)
        page = _single_page(spec, n_pages)
        if re.match(r"(replace|change)\b", low):
            if len(q) >= 2:
                op = new_op("replace_text", find=q[0], replace=q[1], page=page,
                            pages=spec if page is None and spec else "all")
                ops.append(op)
            else:
                notes.append(f"“{s}”: put the old and new text in quotes, e.g. replace \"Old\" with \"New\".")
        elif re.match(r"(delete|remove|erase)\b", low) and re.search(r"\bpages?\b", low) and not q:
            if spec and spec != "all":
                ops.append(new_op("delete_pages", pages=spec))
            else:
                notes.append(f"“{s}”: which pages? e.g. delete page 3.")
        elif re.match(r"(delete|remove|erase)\b", low):
            if q:
                ops.append(new_op("delete_text", find=q[0], page=page, pages=spec or "all"))
            else:
                notes.append(f"“{s}”: put the text to delete in quotes.")
        elif low.startswith("redact"):
            if q:
                ops.append(new_op("redact", terms=q, pages=spec or "all"))
            else:
                notes.append(f"“{s}”: put the terms to redact in quotes (areas are drawn in the editor).")
        elif re.match(r"(highlight|underline|strike)", low):
            kind = "highlight" if low.startswith("highlight") else "underline" if low.startswith("underline") \
                else "strikeout"
            if q:
                ops.append(new_op("markup", kind=kind, find=q[0], page=page, pages=spec or "all"))
            else:
                notes.append(f"“{s}”: put the text to {kind} in quotes.")
        elif "watermark" in low:
            ops.append(new_op("watermark", text=q[0] if q else "CONFIDENTIAL", pages=spec or "all"))
            if not q:
                notes.append("Watermark text not given; proposed “CONFIDENTIAL”.")
        elif "page number" in low:
            pos = next((p for p in ("bottom-right", "bottom-left", "top-right", "top-left", "top-center")
                        if p.replace("-", " ") in low), "bottom-center")
            ops.append(new_op("page_numbers", position=pos, pages=spec or "all"))
        elif low.startswith("rotate"):
            ang = 180 if "180" in low else 270 if ("270" in low or "left" in low or "counter" in low) else 90
            ops.append(new_op("rotate_pages", pages=spec or "all", angle=ang))
        elif low.startswith(("header", "add header")) and q:
            ops.append(new_op("header_footer", header=q[0], pages=spec or "all"))
        elif low.startswith(("footer", "add footer")) and q:
            ops.append(new_op("header_footer", footer=q[0], pages=spec or "all"))
        elif re.match(r"(add|insert|write) (text|note)", low) and q:
            ops.append(new_op("add_text", page=page if page is not None else 0, text=q[0]))
            notes.append("Text box placed at the top-left; drag it to the right position.")
        elif re.match(r"(set )?(title|author|subject|keywords)\b", low) and q:
            key = re.match(r"(?:set )?(title|author|subject|keywords)", low).group(1)
            ops.append(new_op("set_metadata", **{key: q[0]}))
        elif re.match(r"duplicate page", low) and page is not None:
            ops.append(new_op("duplicate_page", page=page))
        else:
            notes.append(f"“{s}”: not understood. Nothing proposed for this part.")
    return ops, notes
