"""What changed in the game's code between two builds, narrowed to what the mods use.

Input: two decompiled trees of the game's assemblies (``ilspycmd -p`` output, one ``.cs`` file
per type). Output, in a folder Claude reads during a fix:

* ``summary.md``: counts and the changed or removed types that the mods' sources name;
* ``relevant.diff``: unified diffs of those types (capped in size);
* ``changed-files.txt``: every changed, added and removed file, for grepping.

The decompiled game code stays on the local machine (it is the game's code, not ours): it is
written under the cache folder only and never committed or pushed.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

__all__ = ["DiffReport", "collect_identifiers", "diff_trees", "write_report"]

_IDENT = re.compile(r"\b[A-Z][A-Za-z0-9_]{2,}\b")


def collect_identifiers(source_dirs: Iterable[Path], suffixes: Tuple[str, ...] = (".cs", ".mjs", ".js")) -> Set[str]:
    """Capitalised identifiers in the mods' sources (type and member names they may use)."""
    found: Set[str] = set()
    for root in source_dirs:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.suffix.lower() in suffixes and p.is_file() and ".git" not in p.parts and "bin" not in p.parts and "obj" not in p.parts:
                try:
                    found.update(_IDENT.findall(p.read_text(encoding="utf-8", errors="replace")))
                except OSError:
                    continue
    return found


def _type_name(rel: str) -> str:
    stem = Path(rel).stem
    return re.sub(r"`\d+$", "", stem).split(".")[-1]


def _hash(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()


def _index(root: Path) -> Dict[str, Path]:
    out: Dict[str, Path] = {}
    if not root.is_dir():
        return out
    for p in root.rglob("*.cs"):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = p
    return out


@dataclass
class DiffReport:
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    changed: List[str] = field(default_factory=list)
    relevant_changed: List[str] = field(default_factory=list)
    relevant_removed: List[str] = field(default_factory=list)
    relevant_diff: str = ""
    truncated: bool = False

    @property
    def total(self) -> int:
        return len(self.added) + len(self.removed) + len(self.changed)


def diff_trees(old_root: Path, new_root: Path, identifiers: Set[str], max_diff_bytes: int = 400_000) -> DiffReport:
    old = _index(old_root)
    new = _index(new_root)
    rep = DiffReport()
    rep.added = sorted(set(new) - set(old))
    rep.removed = sorted(set(old) - set(new))
    for rel in sorted(set(old) & set(new)):
        if _hash(old[rel]) != _hash(new[rel]):
            rep.changed.append(rel)
    rep.relevant_changed = [r for r in rep.changed if _type_name(r) in identifiers]
    rep.relevant_removed = [r for r in rep.removed if _type_name(r) in identifiers]
    chunks: List[str] = []
    size = 0
    for rel in rep.relevant_changed:
        a = old[rel].read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        b = new[rel].read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        text = "".join(difflib.unified_diff(a, b, fromfile="old/" + rel, tofile="new/" + rel, n=3))
        if size + len(text) > max_diff_bytes:
            rep.truncated = True
            chunks.append("# ... diff of %s and later files left out (size cap); compare the trees directly\n" % rel)
            break
        chunks.append(text)
        size += len(text)
    for rel in rep.relevant_removed:
        chunks.append("# REMOVED in the new build: %s\n" % rel)
    rep.relevant_diff = "".join(chunks)
    return rep


def write_report(rep: DiffReport, out_dir: Path, old_label: str, new_label: str, old_root: Path, new_root: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "relevant.diff").write_text(rep.relevant_diff, encoding="utf-8")
    lines = []
    for tag, items in (("A", rep.added), ("D", rep.removed), ("M", rep.changed)):
        lines.extend("%s %s" % (tag, i) for i in items)
    (out_dir / "changed-files.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    md = [
        "# Game code changes %s -> %s" % (old_label, new_label),
        "",
        "Decompiled trees (read-only, local only, never copy large parts into a mod):",
        "",
        "- old: `%s`" % old_root,
        "- new: `%s`" % new_root,
        "",
        "%d files changed, %d added, %d removed in total." % (len(rep.changed), len(rep.added), len(rep.removed)),
        "",
        "## Changed types that the mods name (%d)" % len(rep.relevant_changed),
        "",
    ]
    md.extend("- `%s`" % r for r in rep.relevant_changed[:300])
    md += ["", "## Removed types that the mods name (%d)" % len(rep.relevant_removed), ""]
    md.extend("- `%s`" % r for r in rep.relevant_removed[:300])
    md += ["", "Diffs of the changed types: `relevant.diff`%s." % (" (cut at the size cap)" if rep.truncated else ""),
           "Every changed file: `changed-files.txt` (A = added, D = removed, M = changed).", ""]
    path = out_dir / "summary.md"
    path.write_text("\n".join(md), encoding="utf-8")
    return path
