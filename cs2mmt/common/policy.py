"""Gate on what an automated change may touch before it can be published.

Nobody reviews the changes while the owner is away, and bug reports come from anyone on the
internet, so a report could try to talk Claude into adding something harmful ("prompt
injection"). Whatever Claude was told, the diff it produced has to pass these rules:

* only source files may change (C#, the hand-written UI module, locale text); never build files
  (``*.csproj``, ``*.props``, ``*.targets``: they can run commands at build time), scripts,
  binaries, CI configuration, or Claude's own settings;
* added lines may not use networking, processes, native code, dynamic assembly loading, the
  registry or file deletion (C#), nor fetch/eval/dynamic import (UI JavaScript);
* the change must stay small.

The rules look only at added lines, so code that already uses such an API is not flagged.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = ["FileChange", "parse_unified_diff", "PolicyConfig", "PolicyResult", "check_diff"]


@dataclass
class FileChange:
    path: str
    old_path: Optional[str] = None
    status: str = "modified"  # added / deleted / modified / renamed / binary
    added: List[Tuple[int, str]] = field(default_factory=list)  # (new line number, text)
    removed: List[str] = field(default_factory=list)
    mode_change: Optional[str] = None


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _strip_prefix(p: str) -> str:
    p = p.strip()
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p.startswith("a/") or p.startswith("b/"):
        return p[2:]
    return p


def parse_unified_diff(text: str) -> List[FileChange]:
    """Parses ``git diff`` output (any context size)."""
    files: List[FileChange] = []
    cur: Optional[FileChange] = None
    new_line = 0
    in_hunk = False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r'^diff --git (?:"?a/(.+?)"?) (?:"?b/(.+?)"?)$', line)
            path = m.group(2) if m else line.split(" b/")[-1]
            cur = FileChange(path=path)
            files.append(cur)
            in_hunk = False
            continue
        if cur is None:
            continue
        if line.startswith("new file mode"):
            cur.status = "added"
            continue
        if line.startswith("deleted file mode"):
            cur.status = "deleted"
            continue
        if line.startswith("old mode") or line.startswith("new mode"):
            cur.mode_change = line
            continue
        if line.startswith("rename from "):
            cur.old_path = line[len("rename from "):]
            cur.status = "renamed"
            continue
        if line.startswith("rename to "):
            cur.path = line[len("rename to "):]
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            cur.status = "binary"
            continue
        if line.startswith("--- ") and not in_hunk:
            continue
        if line.startswith("+++ ") and not in_hunk:
            target = _strip_prefix(line[4:])
            if target != "/dev/null":
                cur.path = target
            continue
        m = _HUNK.match(line)
        if m:
            new_line = int(m.group(1))
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+"):
            cur.added.append((new_line, line[1:]))
            new_line += 1
        elif line.startswith("-"):
            cur.removed.append(line[1:])
        elif line.startswith(" ") or line == "":
            new_line += 1
        elif line.startswith("\\"):
            pass  # "\ No newline at end of file"
    return files


# Patterns for added lines, per file kind. Kept deliberately broad: a false alarm only means
# the owner is asked, a miss could ship something harmful to every player.
_CS_FORBIDDEN: Sequence[Tuple[str, str]] = (
    (r"\bSystem\.Net\b|\bHttpClient\b|\bWebClient\b|\bWebRequest\b|\bHttpWebRequest\b|\bTcpClient\b|\bUdpClient\b|\bSockets?\b|\bUnityWebRequest\b", "network access"),
    (r"\bSystem\.Diagnostics\.Process\b|\bProcess\.Start\b|\bProcessStartInfo\b", "starting processes"),
    (r"\[\s*DllImport\b|\bLibraryImport\b|\bMarshal\.GetDelegateForFunctionPointer\b|\bextern\s+(?!alias)\w", "native code"),
    (r"\bAssembly\.(Load|LoadFrom|LoadFile|UnsafeLoadFrom)\b|\bAppDomain\b|\bSystem\.Reflection\.Emit\b|\bActivator\.CreateInstanceFrom\b", "loading code at run time"),
    (r"\bMicrosoft\.Win32\b|\bRegistry\.\w", "the Windows registry"),
    (r"\b(File|Directory)\.(Delete|Move|Replace)\b|\bFileInfo\b.*\.Delete\b", "deleting or moving files"),
    (r"\bEnvironment\.(Exit|FailFast|SetEnvironmentVariable)\b|\bApplication\.Quit\b", "ending the game or changing the environment"),
    (r"\bConvert\.FromBase64String\b|\bSystem\.Security\.Cryptography\b", "decoding hidden payloads"),
    (r"https?://", "a web address in code"),
)
_JS_FORBIDDEN: Sequence[Tuple[str, str]] = (
    (r"\bfetch\s*\(|\bXMLHttpRequest\b|\bWebSocket\b|\bEventSource\b|\bnavigator\.sendBeacon\b", "network access"),
    (r"\beval\s*\(|\bnew\s+Function\s*\(|\bsetTimeout\s*\(\s*['\"`]|\bsetInterval\s*\(\s*['\"`]", "running strings as code"),
    (r"\bimport\s*\(", "dynamic import"),
    (r"https?://", "a web address in code"),
    (r"\batob\s*\(|\bString\.fromCharCode\s*\(", "decoding hidden payloads"),
)
_MD_FORBIDDEN: Sequence[Tuple[str, str]] = (
    (r"https?://", "a new web address in the documentation"),
)


@dataclass
class PolicyConfig:
    allowed_globs: Sequence[str] = (
        "*.cs",
        "**/*.cs",
        "UI/*.mjs",
        "UI/**/*.mjs",
        "UI/*.js",
        "UI/**/*.js",
        "UI/*.css",
        "UI/**/*.css",
        "docs/*.md",
        "docs/**/*.md",
        "README.md",
        "Properties/PublishConfiguration.xml",
    )
    forbidden_globs: Sequence[str] = (
        "*.csproj",
        "**/*.csproj",
        "*.props",
        "**/*.props",
        "*.targets",
        "**/*.targets",
        "Directory.Build.*",
        "**/Directory.Build.*",
        "*.sln",
        "nuget.config",
        "NuGet.Config",
        ".github/**",
        ".claude/**",
        "**/.claude/**",
        "CLAUDE.md",
        "**/CLAUDE.md",
        ".mcp.json",
        "*.ps1",
        "**/*.ps1",
        "*.bat",
        "**/*.bat",
        "*.cmd",
        "**/*.cmd",
        "*.sh",
        "**/*.sh",
        "*.dll",
        "**/*.dll",
        "*.exe",
        "**/*.exe",
        ".gitignore",
        ".gitattributes",
        "LICENSE",
        "LICENSE.*",
    )
    max_files: int = 25
    max_changed_lines: int = 1500
    allow_deletions: bool = False
    # Files only the tool itself edits (version, change log); Claude's edits to them are refused.
    tool_owned: Sequence[str] = ("Properties/PublishConfiguration.xml",)


@dataclass
class PolicyResult:
    ok: bool
    violations: List[str]
    files: int
    added_lines: int
    removed_lines: int

    def summary(self) -> str:
        head = "passed" if self.ok else "FAILED"
        text = "policy %s: %d files, +%d/-%d lines" % (head, self.files, self.added_lines, self.removed_lines)
        if self.violations:
            text += "\n" + "\n".join("  - " + v for v in self.violations)
        return text


def _matches(path: str, globs: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, g) for g in globs)


_TOKENISH = re.compile(r"[A-Za-z0-9+/=_\-]{32,}")


def _entropy(s: str) -> float:
    import math

    if not s:
        return 0.0
    counts: Dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def looks_like_secret(text: str) -> Optional[str]:
    """A long random-looking run of characters (a token, a key, an encoded payload)."""
    for m in _TOKENISH.finditer(text):
        s = m.group(0)
        if s.isdigit() or re.fullmatch(r"[a-f0-9]{40}", s) and False:
            continue
        has_mix = sum(bool(re.search(p, s)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]")) >= 3
        if has_mix and _entropy(s) >= 4.0:
            return s
    return None


def check_diff(
    changes: List[FileChange],
    config: Optional[PolicyConfig] = None,
    tool_edits_allowed: bool = False,
    secrets: Sequence[str] = (),
) -> PolicyResult:
    """Checks parsed changes. ``tool_edits_allowed``: the diff includes the tool's own version edits.

    ``secrets``: values that must never appear in a change (the agent's own tokens), so even a
    session tricked into reading one could not ship it.
    """
    cfg = config or PolicyConfig()
    violations: List[str] = []
    secret_values = [s for s in secrets if s and len(s) >= 8]
    added = sum(len(c.added) for c in changes)
    removed = sum(len(c.removed) for c in changes)
    if len(changes) > cfg.max_files:
        violations.append("%d files changed (limit %d)" % (len(changes), cfg.max_files))
    if added + removed > cfg.max_changed_lines:
        violations.append("%d lines changed (limit %d)" % (added + removed, cfg.max_changed_lines))
    for c in changes:
        paths = [c.path] + ([c.old_path] if c.old_path else [])
        for p in paths:
            if _matches(p, cfg.forbidden_globs):
                violations.append("%s: this kind of file may not be changed automatically" % p)
            elif not _matches(p, cfg.allowed_globs):
                violations.append("%s: not in the list of files that may change" % p)
            elif _matches(p, cfg.tool_owned) and not tool_edits_allowed:
                violations.append("%s: only the tool itself edits this file" % p)
        if c.status == "binary":
            violations.append("%s: binary change" % c.path)
        if c.status == "deleted" and not cfg.allow_deletions:
            violations.append("%s: file deleted" % c.path)
        if c.mode_change:
            violations.append("%s: file mode changed (%s)" % (c.path, c.mode_change))
        rules: Sequence[Tuple[str, str]] = ()
        lower = c.path.lower()
        if lower.endswith(".cs"):
            rules = _CS_FORBIDDEN
        elif lower.endswith((".mjs", ".js")):
            rules = _JS_FORBIDDEN
        elif lower.endswith(".md"):
            rules = _MD_FORBIDDEN
        for line_no, text in c.added:
            stripped = text.strip()
            if any(s in text for s in secret_values):
                violations.append("%s:%d: contains one of the agent's own credentials" % (c.path, line_no))
                continue
            tok = looks_like_secret(text) if not lower.endswith(".md") else None
            if tok is not None:
                violations.append("%s:%d: a long random-looking string (key, token or encoded data?): %s..." % (c.path, line_no, tok[:12]))
                continue
            if stripped.startswith("//") and "http" in stripped and not re.search(r"\b(fetch|Process|DllImport)\b", stripped):
                # A web address in a code comment (a reference to a spec) is harmless.
                continue
            for rx, what in rules:
                if re.search(rx, text):
                    violations.append("%s:%d: %s (%s)" % (c.path, line_no, what, stripped[:120]))
                    break
    return PolicyResult(ok=not violations, violations=violations, files=len(changes), added_lines=added, removed_lines=removed)


def policy_from_dict(d: Dict) -> PolicyConfig:
    cfg = PolicyConfig()
    for key in ("max_files", "max_changed_lines"):
        if key in d:
            setattr(cfg, key, int(d[key]))
    if "allow_deletions" in d:
        cfg.allow_deletions = bool(d["allow_deletions"])
    if "extra_allowed_globs" in d:
        cfg.allowed_globs = tuple(cfg.allowed_globs) + tuple(d["extra_allowed_globs"])
    if "extra_forbidden_globs" in d:
        cfg.forbidden_globs = tuple(cfg.forbidden_globs) + tuple(d["extra_forbidden_globs"])
    return cfg
