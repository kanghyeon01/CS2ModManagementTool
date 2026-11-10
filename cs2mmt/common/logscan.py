"""Reading the game's logs after a smoke test and deciding, per mod, whether it ran cleanly.

The game writes each mod's own log to ``Logs/<Name>.log`` (Colossal.Logging) and Unity's
``Player.log`` next to the ``Logs`` folder. An error belongs to a mod when the error line or the
stack trace under it names one of the mod's identifiers (its namespace or assembly name).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

__all__ = ["LogFile", "ModVerdict", "scan_logs", "ERROR_MARKERS"]

ERROR_MARKERS = re.compile(
    r"(\[ERROR\]|\[CRITICAL\]|\[FATAL\]|[A-Za-z]*Exception\b(?!s? (?:handled|ignored))|\bERROR\b|Unknown element type|Error: )"
)
# Lines that look like errors but are not (the game's own noise is filtered per mod by identifiers).
_NOT_AN_ERROR = re.compile(r"(no errors|0 errors|ErrorCount\s*=\s*0|errors?: 0\b)", re.I)
STACK_FRAME = re.compile(r"^\s+(at |\S+\.\S+\s*\(|\[0x[0-9a-f]+\])")


@dataclass
class LogFile:
    name: str
    text: str


@dataclass
class ModVerdict:
    mod_id: str
    loaded: bool
    expectations: Dict[str, bool] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    log_found: bool = False

    @property
    def ok(self) -> bool:
        return self.loaded and all(self.expectations.values()) and not self.errors

    def summary(self) -> str:
        parts = ["loaded" if self.loaded else "NOT LOADED"]
        missing = [k for k, v in self.expectations.items() if not v]
        if missing:
            parts.append("missing log lines: " + ", ".join(missing))
        if self.errors:
            parts.append("%d error(s)" % len(self.errors))
        return "%s: %s" % (self.mod_id, "; ".join(parts))


def _blocks(text: str) -> Iterable[List[str]]:
    """Error line plus the stack-trace lines under it."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if ERROR_MARKERS.search(line) and not _NOT_AN_ERROR.search(line):
            block = [line]
            j = i + 1
            while j < len(lines) and len(block) < 40 and (STACK_FRAME.match(lines[j]) or lines[j].startswith(("  ", "\t", "Rethrow", "--- End"))):
                block.append(lines[j])
                j += 1
            yield block
            i = j
        else:
            i += 1


def scan_logs(
    logs: Sequence[LogFile],
    mods: Sequence[dict],
    harness_errors: Sequence[dict] = (),
    ignore: Sequence[str] = (),
) -> List[ModVerdict]:
    """``mods``: dicts with id, identifiers, log_names, smoke_expect (from ModSpec)."""
    ignore_rx = [re.compile(p) for p in ignore]
    by_name = {l.name.lower(): l for l in logs}
    verdicts: List[ModVerdict] = []
    for mod in mods:
        ids = [i for i in mod.get("identifiers", []) if i]
        id_rx = re.compile("|".join(re.escape(i) for i in ids)) if ids else None
        own_logs: List[LogFile] = []
        for name in mod.get("log_names", []):
            lf = by_name.get((name + ".log").lower()) or by_name.get(name.lower())
            if lf is not None:
                own_logs.append(lf)
        own_text = "\n".join(l.text for l in own_logs)
        v = ModVerdict(mod_id=mod["id"], loaded=False, log_found=bool(own_logs))
        v.loaded = bool(own_logs) and ("OnLoad" in own_text or bool(own_text.strip()))
        for rx in mod.get("smoke_expect", []):
            v.expectations[rx] = re.search(rx, own_text) is not None
        seen = set()
        for lf in logs:
            is_own = lf in own_logs
            for block in _blocks(lf.text):
                joined = "\n".join(block)
                if any(r.search(joined) for r in ignore_rx):
                    continue
                if is_own or (id_rx is not None and id_rx.search(joined)):
                    key = block[0].strip()[-200:]
                    if key in seen:
                        continue
                    seen.add(key)
                    v.errors.append("[%s] %s" % (lf.name, joined[:1500]))
        for he in harness_errors:
            text = "%s\n%s" % (he.get("message", ""), he.get("stack", ""))
            if id_rx is not None and id_rx.search(text) and not any(r.search(text) for r in ignore_rx):
                key = str(he.get("message", ""))[-200:]
                if key not in seen:
                    seen.add(key)
                    v.errors.append("[harness %s] %s" % (he.get("type", ""), text[:1500]))
        verdicts.append(v)
    return verdicts


def read_logs(paths: Iterable[Path], since_epoch: Optional[float] = None, max_bytes: int = 4_000_000) -> List[LogFile]:
    out: List[LogFile] = []
    for p in paths:
        try:
            st = p.stat()
        except OSError:
            continue
        if since_epoch is not None and st.st_mtime + 2 < since_epoch:
            continue
        with open(p, "rb") as f:
            if st.st_size > max_bytes:
                f.seek(st.st_size - max_bytes)
            data = f.read()
        out.append(LogFile(p.name, data.decode("utf-8", errors="replace")))
    return out
