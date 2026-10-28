"""Structured JSON-lines logging. Never log document text, passwords, cookies or tokens."""
from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path

_FIELDS = ("job_id", "profile_id", "input_path", "output_path", "mode", "step", "duration_ms", "detected_ns",
           "validation", "timestamps", "error_code", "recovery", "state")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
             "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        for f in _FIELDS:
            v = getattr(record, f, None)
            if v is not None:
                d[f] = v
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)[-2000:]
        return json.dumps(d, default=str)


def setup_logging(log_dir: Path, level=logging.INFO) -> logging.Logger:
    root = logging.getLogger("prm")
    target = str((log_dir / "prm.log.jsonl").resolve())
    for old in [h for h in root.handlers if getattr(h, "_prm", False)]:
        if old.baseFilename == target:
            return root
        root.removeHandler(old)
        old.close()
    log_dir.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(target, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(JsonFormatter())
    h._prm = True
    root.addHandler(h)
    root.setLevel(level)
    return root


def get_logger(name: str = "prm") -> logging.Logger:
    return logging.getLogger(name if name.startswith("prm") else f"prm.{name}")
