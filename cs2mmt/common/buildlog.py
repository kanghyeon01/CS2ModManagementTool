"""Parsing MSBuild / C# compiler output into a short list of diagnostics."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

__all__ = ["Diagnostic", "parse_msbuild", "format_diagnostics"]


@dataclass(frozen=True)
class Diagnostic:
    kind: str  # "error" or "warning"
    code: str  # CS0117, MSB4019, or "" for project errors without a code
    message: str
    file: str = ""
    line: int = 0
    column: int = 0

    def short(self) -> str:
        where = ""
        if self.file:
            where = "%s(%d,%d): " % (self.file, self.line, self.column) if self.line else self.file + ": "
        return "%s%s %s: %s" % (where, self.kind, self.code or "", self.message)


_WITH_LOCATION = re.compile(
    r"^\s*(?P<file>.+?)\((?P<line>\d+)(?:,(?P<col>\d+))?(?:,\d+,\d+)?\)\s*:\s*"
    r"(?P<kind>error|warning)\s*(?P<code>[A-Z]{2,}\d+)?\s*:\s*(?P<msg>.*?)\s*(?:\[(?P<proj>[^\]]+)\])?\s*$"
)
_WITHOUT_LOCATION = re.compile(
    r"^\s*(?:(?P<file>[^:\[]+?)\s*:\s*)?(?P<kind>error|warning)\s+(?P<code>[A-Z]{2,}\d+)\s*:\s*(?P<msg>.*?)\s*(?:\[(?P<proj>[^\]]+)\])?\s*$"
)


def parse_msbuild(output: str) -> List[Diagnostic]:
    seen = set()
    out: List[Diagnostic] = []
    for raw in output.splitlines():
        line = raw.rstrip()
        m = _WITH_LOCATION.match(line)
        if m:
            d = Diagnostic(
                kind=m.group("kind"),
                code=m.group("code") or "",
                message=m.group("msg").strip(),
                file=m.group("file").strip(),
                line=int(m.group("line")),
                column=int(m.group("col") or 0),
            )
        else:
            m = _WITHOUT_LOCATION.match(line)
            if not m:
                continue
            d = Diagnostic(kind=m.group("kind"), code=m.group("code"), message=m.group("msg").strip(), file=(m.group("file") or "").strip())
        key = (d.kind, d.code, d.message, d.file, d.line, d.column)
        if key in seen:
            continue  # MSBuild repeats every diagnostic in its summary
        seen.add(key)
        out.append(d)
    return out


def format_diagnostics(diags: List[Diagnostic], limit: int = 60, kind: str = "error") -> str:
    selected = [d for d in diags if d.kind == kind]
    lines = [d.short() for d in selected[:limit]]
    if len(selected) > limit:
        lines.append("... and %d more" % (len(selected) - limit))
    return "\n".join(lines)
