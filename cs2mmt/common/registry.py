"""The mod registry (``config/mods.toml``).

Every mod the tool looks after is one ``[[mod]]`` table. Adding a mod means adding a table; no
code changes. Values not given fall back to ``[defaults]`` and then to the built-in defaults.
"""

from __future__ import annotations

import re
from . import toml_compat as tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = ["RegistryError", "VersionLocation", "ModSpec", "Registry", "load_registry"]


class RegistryError(ValueError):
    pass


@dataclass
class VersionLocation:
    """A file that carries the mod version, e.g. the UI module header ``* Version: 1.3.0``."""

    path: str
    pattern: str  # regex with a named group "v"; matched per line (MULTILINE)

    def compiled(self) -> "re.Pattern[str]":
        rx = re.compile(self.pattern, re.M)
        if "v" not in rx.groupindex:
            raise RegistryError("version pattern %r has no (?P<v>...) group" % self.pattern)
        return rx


@dataclass
class ModSpec:
    id: str
    display_name: str
    repo: str
    dir: str
    project: str
    assembly: str
    publish_config: str = "Properties/PublishConfiguration.xml"
    default_branch: str = "main"
    depends_on: List[str] = field(default_factory=list)
    release_with: List[str] = field(default_factory=list)
    forum_threads: List[str] = field(default_factory=list)
    github_issues: bool = True
    identifiers: List[str] = field(default_factory=list)
    log_names: List[str] = field(default_factory=list)
    smoke_expect: List[str] = field(default_factory=list)
    version_files: List[VersionLocation] = field(default_factory=list)
    readme_game_version: Optional[VersionLocation] = None
    publish: bool = True
    content_dir: str = ""  # default: the deployed local Mods/<assembly> folder
    extra_build_props: Dict[str, str] = field(default_factory=dict)
    test_mods: List[str] = field(default_factory=list)
    owner_names: List[str] = field(default_factory=list)
    notes: str = ""

    @property
    def github_slug(self) -> Optional[str]:
        m = re.match(r"^https?://github\.com/([^/]+)/([^/#?]+?)(?:\.git)?/?$", self.repo.strip())
        return None if m is None else "%s/%s" % (m.group(1), m.group(2))

    def all_identifiers(self) -> List[str]:
        ids = list(self.identifiers) or [self.assembly]
        if self.assembly not in ids:
            ids.append(self.assembly)
        return ids

    def all_log_names(self) -> List[str]:
        return list(self.log_names) or [self.assembly]


_KNOWN_KEYS = set(ModSpec.__dataclass_fields__.keys())


@dataclass
class Registry:
    mods: List[ModSpec]
    defaults: Dict[str, Any] = field(default_factory=dict)

    def get(self, mod_id: str) -> ModSpec:
        for m in self.mods:
            if m.id == mod_id:
                return m
        raise KeyError(mod_id)

    def ids(self) -> List[str]:
        return [m.id for m in self.mods]

    def build_order(self, only: Optional[List[str]] = None) -> List[ModSpec]:
        """Mods sorted so that every mod comes after the mods it depends on."""
        wanted = set(only) if only else set(self.ids())
        # A dependency of a wanted mod is built too: the dependent compiles against its DLL.
        changed = True
        while changed:
            changed = False
            for m in self.mods:
                if m.id in wanted:
                    for d in m.depends_on:
                        if d not in wanted:
                            wanted.add(d)
                            changed = True
        order: List[ModSpec] = []
        state: Dict[str, int] = {}

        def visit(mod: ModSpec, chain: List[str]) -> None:
            s = state.get(mod.id, 0)
            if s == 2:
                return
            if s == 1:
                raise RegistryError("dependency cycle: %s" % " -> ".join(chain + [mod.id]))
            state[mod.id] = 1
            for dep in mod.depends_on:
                visit(self.get(dep), chain + [mod.id])
            state[mod.id] = 2
            if mod.id in wanted:
                order.append(mod)

        for m in self.mods:
            visit(m, [])
        return order

    def release_group(self, mod_id: str) -> List[str]:
        """The mods that must be published together with ``mod_id`` (both directions)."""
        group = {mod_id}
        changed = True
        while changed:
            changed = False
            for m in self.mods:
                linked = set(m.release_with) | {m.id}
                if linked & group and not linked <= group:
                    group |= linked
                    changed = True
        return [m.id for m in self.build_order(sorted(group)) if m.id in group]

    def dependents_of(self, mod_id: str) -> List[str]:
        out: List[str] = []
        for m in self.mods:
            if mod_id in m.depends_on:
                out.append(m.id)
        return out


def _version_location(raw: Any, where: str) -> VersionLocation:
    if not isinstance(raw, dict) or "path" not in raw or "pattern" not in raw:
        raise RegistryError("%s: expected a table with 'path' and 'pattern'" % where)
    loc = VersionLocation(path=str(raw["path"]), pattern=str(raw["pattern"]))
    loc.compiled()
    return loc


def _build_mod(raw: Dict[str, Any], defaults: Dict[str, Any], index: int) -> ModSpec:
    merged: Dict[str, Any] = {}
    for k, v in defaults.items():
        if k in _KNOWN_KEYS:
            merged[k] = v
    merged.update(raw)
    unknown = set(raw.keys()) - _KNOWN_KEYS
    if unknown:
        raise RegistryError("mod #%d: unknown keys %s" % (index + 1, ", ".join(sorted(unknown))))
    for key in ("id", "repo", "project", "assembly"):
        if not merged.get(key):
            raise RegistryError("mod #%d: '%s' is required" % (index + 1, key))
    mod_id = str(merged["id"])
    if not re.match(r"^[A-Za-z0-9_.-]+$", mod_id):
        raise RegistryError("mod id %r: use letters, digits, '.', '_' or '-'" % mod_id)
    merged.setdefault("display_name", mod_id)
    merged.setdefault("dir", "mods/" + mod_id)
    vf = [_version_location(x, "%s.version_files" % mod_id) for x in merged.get("version_files", [])]
    merged["version_files"] = vf
    rgv = merged.get("readme_game_version")
    merged["readme_game_version"] = _version_location(rgv, "%s.readme_game_version" % mod_id) if rgv else None
    for key in ("depends_on", "release_with", "forum_threads", "identifiers", "log_names", "smoke_expect", "test_mods", "owner_names"):
        value = merged.get(key, [])
        if isinstance(value, str):
            value = [value]
        merged[key] = [str(x) for x in value]
    for rx in merged["smoke_expect"]:
        re.compile(rx)
    merged["extra_build_props"] = {str(k): str(v) for k, v in dict(merged.get("extra_build_props", {})).items()}
    return ModSpec(**merged)


def load_registry(path: Path) -> Registry:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    defaults = dict(data.get("defaults", {}))
    raws = data.get("mod", [])
    if not isinstance(raws, list) or not raws:
        raise RegistryError("%s: no [[mod]] entries" % path)
    mods = [_build_mod(dict(r), defaults, i) for i, r in enumerate(raws)]
    seen = set()
    for m in mods:
        if m.id in seen:
            raise RegistryError("duplicate mod id %r" % m.id)
        seen.add(m.id)
    reg = Registry(mods=mods, defaults=defaults)
    for m in mods:
        for d in m.depends_on + m.release_with:
            if d not in seen:
                raise RegistryError("mod %s refers to unknown mod %r" % (m.id, d))
    reg.build_order()  # raises on cycles
    return reg
