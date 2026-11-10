"""Mod and game version handling.

Mod versions follow MAJOR.MINOR.PATCH and must stay numeric: the game parses the UI module's
``Version:`` header as a number (Realistic Aviation's 1.1a had to become 1.1.1). Two-part
versions such as ``1.1`` are accepted and grow a third part when bumped.

Game versions look like ``1.6.2f1``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

__all__ = [
    "VersionError",
    "parse_mod_version",
    "bump_mod_version",
    "is_numeric_version",
    "GameVersion",
    "parse_game_version",
    "game_version_wildcard",
    "wildcard_matches",
]


class VersionError(ValueError):
    pass


_NUMERIC = re.compile(r"^\d+(\.\d+){0,3}$")


def is_numeric_version(text: str) -> bool:
    return bool(_NUMERIC.match(text.strip()))


def parse_mod_version(text: str) -> Tuple[int, ...]:
    text = text.strip()
    if not is_numeric_version(text):
        raise VersionError("not a numeric version: %r" % text)
    return tuple(int(p) for p in text.split("."))


def bump_mod_version(text: str, part: str = "patch") -> str:
    """Returns the next version. ``part`` is "major", "minor" or "patch"."""
    parts = list(parse_mod_version(text))
    while len(parts) < 3:
        parts.append(0)
    if part == "major":
        parts = [parts[0] + 1, 0, 0] + [0] * (len(parts) - 3)
    elif part == "minor":
        parts = [parts[0], parts[1] + 1, 0] + [0] * (len(parts) - 3)
    elif part == "patch":
        parts[2] += 1
        for i in range(3, len(parts)):
            parts[i] = 0
    else:
        raise VersionError("unknown version part: %r" % part)
    # Keep a fourth component only if the input had one.
    return ".".join(str(p) for p in parts[: max(3, len(text.strip().split(".")))])


@dataclass(frozen=True, order=True)
class GameVersion:
    major: int
    minor: int
    patch: int
    suffix: str = ""

    def __str__(self) -> str:
        return "%d.%d.%d%s" % (self.major, self.minor, self.patch, self.suffix)

    @property
    def minor_line(self) -> Tuple[int, int]:
        return (self.major, self.minor)


_GAME = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?([A-Za-z][A-Za-z0-9]*)?")


def parse_game_version(text: Optional[str]) -> Optional[GameVersion]:
    """Parses '1.6.2f1' (also '1.6.2', '1.6') from any text; None when there is none."""
    if not text:
        return None
    m = _GAME.search(text)
    if not m:
        return None
    return GameVersion(int(m.group(1)), int(m.group(2)), int(m.group(3) or 0), m.group(4) or "")


def game_version_wildcard(version: GameVersion) -> str:
    """The PublishConfiguration GameVersion value for a game version, e.g. '1.6.*'."""
    return "%d.%d.*" % (version.major, version.minor)


def wildcard_matches(pattern: str, version: GameVersion) -> bool:
    """True when a GameVersion value such as '1.6.*' or '1.*' or '1.6.2' covers ``version``."""
    pattern = pattern.strip()
    if not pattern or pattern == "*":
        return True
    parts = pattern.split(".")
    actual = [str(version.major), str(version.minor), "%d%s" % (version.patch, version.suffix)]
    for i, p in enumerate(parts):
        if p == "*":
            return True
        if i >= len(actual):
            return False
        if i == 2:
            # '1.6.2' matches '1.6.2f1' (but not '1.6.20'); '1.6.2f1' must match exactly.
            if p.isdigit():
                if int(p) != version.patch:
                    return False
            elif p != actual[2]:
                return False
        elif p != actual[i]:
            return False
    return len(parts) >= 2
