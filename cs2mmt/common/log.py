"""Logging setup: a rotating file plus the console, with secrets scrubbed from every line."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Iterable, List, Optional

__all__ = ["setup_logging", "Redactor", "redact"]


def redact(text: str, secrets: Iterable[str]) -> str:
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, "***")
    return text


class Redactor(logging.Filter):
    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        self.secrets: List[str] = [s for s in secrets if s and len(s) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            msg = record.getMessage()
            cleaned = redact(msg, self.secrets)
            if cleaned != msg:
                record.msg = cleaned
                record.args = ()
        return True


def setup_logging(log_file: Optional[Path], level: str = "INFO", secrets: Iterable[str] = ()) -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    redactor = Redactor(secrets)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    console.addFilter(redactor)
    root.addHandler(console)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=10, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(redactor)
        root.addHandler(fh)
    return logging.getLogger("cs2mmt")
