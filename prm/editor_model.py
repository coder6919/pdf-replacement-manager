"""Editor document model: base PDF + ordered operations, with undo/redo, rendering cache and autosave.

UI-independent so it can be tested headlessly. The same operation list becomes a profile's saved
operations, or is rendered once into a saved master.
"""
from __future__ import annotations

import copy
import json
import time
import uuid
from pathlib import Path

import pymupdf

from .fsutil import sha256_bytes, write_atomic
from .operations import (REGISTRY, ApplyContext, OpResult, apply_operations, media_to_page, page_to_media,
                         simulate_pages, validate_op)

UNDO_LIMIT = 200


class EditSession:
    def __init__(self, base_pdf: bytes, ops: list | None = None, *, base_name: str = "document.pdf",
                 asset_resolver=None, session_dir: Path | None = None, session_id: str | None = None,
                 profile_id: int | None = None):
        self.base_pdf = base_pdf
        self.base_sha = sha256_bytes(base_pdf)
        self.base_name = base_name
        self.ops: list[dict] = copy.deepcopy(ops or [])
        self.asset_resolver = asset_resolver
        self.session_dir = Path(session_dir) if session_dir else None
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.profile_id = profile_id
        self._undo: list[list] = []
        self._redo: list[list] = []
        self._doc: pymupdf.Document | None = None
        self._results: list[OpResult] = []
        self._orig: pymupdf.Document | None = None
        self.dirty = False
        self.revision = 0
        doc = pymupdf.open(stream=base_pdf, filetype="pdf")
        if doc.needs_pass:
            doc.close()
            raise ValueError("Password-protected PDFs cannot be edited.")
        self.base_pages = doc.page_count
        doc.close()

    # ------------------------------------------------------------------ history
    def _push(self):
        self._undo.append(copy.deepcopy(self.ops))
        if len(self._undo) > UNDO_LIMIT:
            self._undo.pop(0)
        self._redo.clear()

    def _changed(self, incremental_op: dict | None = None):
        self.dirty = True
        self.revision += 1
        if incremental_op is not None and self._doc is not None and all(r.ok for r in self._results):
            res = apply_operations(self._doc, [incremental_op], self._ctx(), stop_on_error=False)
            self._results.extend(res)
            if res and not res[0].ok:
                self._invalidate()
        else:
            self._invalidate()
        self.autosave()

    def _invalidate(self):
        if self._doc is not None:
            self._doc.close()
        self._doc = None

    def can_undo(self):
        return bool(self._undo)

    def can_redo(self):
        return bool(self._redo)

    def undo(self):
        if self._undo:
            self._redo.append(copy.deepcopy(self.ops))
            self.ops = self._undo.pop()
            self._changed()

    def redo(self):
        if self._redo:
            self._undo.append(copy.deepcopy(self.ops))
            self.ops = self._redo.pop()
            self._changed()

    # ------------------------------------------------------------------ editing
    def add(self, op: dict) -> list[str]:
        errs = validate_op(op)
        if errs:
            return errs
        self._push()
        self.ops.append(copy.deepcopy(op))
        self._changed(incremental_op=op if op.get("enabled", True) else None)
        return []

    def add_many(self, ops: list[dict]) -> list[str]:
        errs = [e for op in ops for e in validate_op(op)]
        if errs:
            return errs
        self._push()
        self.ops.extend(copy.deepcopy(ops))
        self._changed()
        return []

    def find(self, op_id: str) -> dict | None:
        return next((o for o in self.ops if o["id"] == op_id), None)

    def update(self, op_id: str, **changes) -> list[str]:
        op = self.find(op_id)
        if op is None:
            return ["Operation not found."]
        trial = {**op, **changes}
        errs = validate_op(trial)
        if errs:
            return errs
        self._push()
        op.update(changes)
        self._changed()
        return []

    def remove(self, op_id: str):
        if self.find(op_id):
            self._push()
            self.ops = [o for o in self.ops if o["id"] != op_id]
            self._changed()

    def move(self, op_id: str, delta: int):
        i = next((k for k, o in enumerate(self.ops) if o["id"] == op_id), None)
        j = None if i is None else i + delta
        if i is None or j < 0 or j >= len(self.ops):
            return
        self._push()
        self.ops.insert(j, self.ops.pop(i))
        self._changed()

    # ------------------------------------------------------------------ rendering
    def _ctx(self):
        return ApplyContext(asset_resolver=self.asset_resolver, file_name=self.base_name,
                            download_ns=time.time_ns(), processing_ns=time.time_ns())

    def document(self) -> pymupdf.Document:
        if self._doc is None:
            self._doc = pymupdf.open(stream=self.base_pdf, filetype="pdf")
            self._results = apply_operations(self._doc, self.ops, self._ctx(), stop_on_error=False)
        return self._doc

    def original(self) -> pymupdf.Document:
        if self._orig is None:
            self._orig = pymupdf.open(stream=self.base_pdf, filetype="pdf")
        return self._orig

    def results(self) -> dict[str, OpResult]:
        self.document()
        return {r.op_id: r for r in self._results}

    @property
    def page_count(self) -> int:
        return self.document().page_count

    def output_bytes(self) -> bytes:
        failed = [r for r in self.results().values() if not r.ok]
        if failed:
            raise ValueError(f"{len(failed)} operation(s) failed; fix or disable them first.")
        return self.document().tobytes(garbage=1, deflate=True)

    def export(self, path: Path | str):
        write_atomic(path, self.output_bytes())

    def export_pages(self, path: Path | str, pages: list[int]):
        """Split/extract: write selected pages of the edited document to a new PDF."""
        src = self.document()
        out = pymupdf.open()
        for p in pages:
            out.insert_pdf(src, from_page=p, to_page=p)
        write_atomic(path, out.tobytes(garbage=1, deflate=True))
        out.close()

    # ------------------------------------------------------------------ geometry mapping
    def final_page_of(self, op_index: int, page: int) -> int | None:
        """Where page `page` (as numbered when ops[op_index] runs) ends up after all later operations."""
        enabled = [o for o in self.ops[:op_index] if o.get("enabled", True)]
        n = simulate_pages(self.base_pages, enabled)
        tokens = list(range(n))
        counter = [10 ** 6]

        def new():
            counter[0] += 1
            return counter[0]
        for o in self.ops[op_index + 1:]:
            spec = REGISTRY.get(o.get("type"))
            if o.get("enabled", True) and spec and spec.transform:
                try:
                    tokens = spec.transform(o, tokens, new)
                except Exception:
                    return None
        return tokens.index(page) if page in tokens else None

    def objects_on_page(self, final_page: int) -> list[tuple[dict, pymupdf.Rect]]:
        """Movable objects (geometric ops) displayed on final_page, with rects in that page's coordinates."""
        doc = self.document()
        if final_page >= doc.page_count:
            return []
        page = doc[final_page]
        out = []
        for i, op in enumerate(self.ops):
            spec = REGISTRY.get(op.get("type"))
            if not spec or not spec.geometric or not op.get("enabled", True) or not isinstance(op.get("page"), int):
                continue
            if self.final_page_of(i, op["page"]) == final_page:
                out.append((op, media_to_page(page, op["rect"])))
        return out

    def origin_of_final(self, final_page: int) -> int | None:
        """Original page index that became final_page (None for inserted pages)."""
        tokens = list(range(self.base_pages))
        counter = [10 ** 6]

        def new():
            counter[0] += 1
            return counter[0]
        for o in self.ops:
            spec = REGISTRY.get(o.get("type"))
            if o.get("enabled", True) and spec and spec.transform:
                try:
                    tokens = spec.transform(o, tokens, new)
                except Exception:
                    return None
        if final_page < len(tokens) and tokens[final_page] < self.base_pages:
            return tokens[final_page]
        return None

    def page_rect_to_media(self, final_page: int, rect) -> list[float]:
        return page_to_media(self.document()[final_page], rect)

    def op_page_for_final(self, final_page: int) -> int:
        """Page index to store in a NEW operation appended now (appended ops see the final layout)."""
        return final_page

    # ------------------------------------------------------------------ autosave
    def session_file(self) -> Path | None:
        return self.session_dir / f"{self.session_id}.json" if self.session_dir else None

    def autosave(self):
        f = self.session_file()
        if not f:
            return
        pdf = self.session_dir / f"{self.base_sha}.pdf"
        if not pdf.exists():
            write_atomic(pdf, self.base_pdf)
        write_atomic(f, json.dumps({"v": 1, "session_id": self.session_id, "base_sha": self.base_sha,
                                    "base_name": self.base_name, "profile_id": self.profile_id,
                                    "saved_ns": time.time_ns(), "ops": self.ops}).encode())

    def discard_autosave(self):
        f = self.session_file()
        if f and f.exists():
            f.unlink()

    @classmethod
    def load_autosave(cls, path: Path, asset_resolver=None) -> "EditSession":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        pdf = Path(path).parent / f"{d['base_sha']}.pdf"
        base = pdf.read_bytes()
        if sha256_bytes(base) != d["base_sha"]:
            raise ValueError("Autosaved base PDF failed its integrity check.")
        s = cls(base, d["ops"], base_name=d["base_name"], asset_resolver=asset_resolver,
                session_dir=Path(path).parent, session_id=d["session_id"], profile_id=d.get("profile_id"))
        s.dirty = True
        return s

    @staticmethod
    def list_autosaves(session_dir: Path) -> list[dict]:
        out = []
        for f in sorted(Path(session_dir).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                out.append({"path": f, "name": d["base_name"], "ops": len(d["ops"]), "saved_ns": d["saved_ns"],
                            "profile_id": d.get("profile_id")})
            except Exception:
                continue
        return out
