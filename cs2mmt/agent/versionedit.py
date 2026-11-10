"""Preparing a release: version number, change log, game version, README line.

Done by the tool, not by Claude, so the version always moves the same way: the PATCH part goes
up by one, every file that carries the version (PublishConfiguration ``ModVersion``, the UI
module's ``Version:`` header, ...) gets the same number, and the change log is written in the
owner's format: the English block, a blank line, then the Korean block, each headed by the
version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from ..common import publishconfig as pc
from ..common.registry import ModSpec
from ..common.versioning import GameVersion, bump_mod_version, game_version_wildcard, wildcard_matches

__all__ = ["ReleaseEdit", "format_changelog", "prepare_release", "update_game_version_only", "read_text", "write_text"]


@dataclass
class ReleaseEdit:
    old_version: str
    new_version: str
    files_changed: List[str] = field(default_factory=list)
    game_version_value: Optional[str] = None


def read_text(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8")


def write_text(path: Path, text: str) -> None:
    """Writes UTF-8, keeping a byte order mark and CRLF line ends if the file had them."""
    bom = False
    crlf = False
    if path.exists():
        old = path.read_bytes()
        bom = old.startswith(b"\xef\xbb\xbf")
        crlf = b"\r\n" in old
    out = text.replace("\r\n", "\n")
    if crlf:
        out = out.replace("\n", "\r\n")
    data = out.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)


def _clean_lines(lines: List[str]) -> List[str]:
    out = []
    for line in lines:
        line = " ".join(str(line).split())
        if not line:
            continue
        if line.startswith(("- ", "* ")):
            line = line[2:]
        out.append("- " + line)
    return out


def format_changelog(version: str, en: List[str], ko: List[str]) -> str:
    en_lines = _clean_lines(en) or ["- Maintenance update."]
    ko_lines = _clean_lines(ko) or ["- 유지보수 업데이트입니다."]
    return "%s\n%s\n\n%s\n%s" % (version, "\n".join(en_lines), version, "\n".join(ko_lines))


def _replace_version(text: str, pattern, new_version: str) -> Optional[str]:
    m = pattern.search(text)
    if not m:
        return None
    start, end = m.span("v")
    return text[:start] + new_version + text[end:]


def prepare_release(
    mod_dir: Path,
    spec: ModSpec,
    changelog_en: List[str],
    changelog_ko: List[str],
    game_version: Optional[GameVersion] = None,
    bump: str = "patch",
) -> ReleaseEdit:
    cfg_path = mod_dir / spec.publish_config
    text = read_text(cfg_path)
    info = pc.read_info(text)
    old = info.mod_version
    new = bump_mod_version(old, bump)
    text = pc.set_value(text, "ModVersion", new)
    text = pc.set_text(text, "ChangeLog", format_changelog(new, changelog_en, changelog_ko), insert_after="ModVersion")
    edit = ReleaseEdit(old_version=old, new_version=new, files_changed=[spec.publish_config])
    if game_version is not None and info.game_version and not wildcard_matches(info.game_version, game_version):
        value = game_version_wildcard(game_version)
        text = pc.set_value(text, "GameVersion", value, insert_after="ChangeLog")
        edit.game_version_value = value
    write_text(cfg_path, text)
    for loc in spec.version_files:
        p = mod_dir / loc.path
        if not p.is_file():
            continue
        body = read_text(p)
        changed = _replace_version(body, loc.compiled(), new)
        if changed is not None and changed != body:
            write_text(p, changed)
            edit.files_changed.append(loc.path)
    if spec.readme_game_version is not None and game_version is not None:
        p = mod_dir / spec.readme_game_version.path
        if p.is_file():
            body = read_text(p)
            changed = _replace_version(body, spec.readme_game_version.compiled(), str(game_version))
            if changed is not None and changed != body:
                write_text(p, changed)
                edit.files_changed.append(spec.readme_game_version.path)
    return edit


def update_game_version_only(mod_dir: Path, spec: ModSpec, game_version: GameVersion) -> Optional[str]:
    """For a mod that needed no change: widen GameVersion when the game left the declared line.

    Returns the new GameVersion value, or None when nothing had to change.
    """
    cfg_path = mod_dir / spec.publish_config
    text = read_text(cfg_path)
    info = pc.read_info(text)
    if not info.game_version or wildcard_matches(info.game_version, game_version):
        return None
    value = game_version_wildcard(game_version)
    write_text(cfg_path, pc.set_value(text, "GameVersion", value))
    return value
