"""Configuration loading.

A config is a TOML file. Secrets live in a second file (``secrets.toml`` next to it, or the path
given by ``secrets_file``) so the main file can be shown or committed without them. Environment
variables ``CS2MMT__<SECTION>__<KEY>`` override single values, which suits Docker.
"""

from __future__ import annotations

import os
from . import toml_compat as tomllib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

__all__ = ["ConfigError", "Config", "load_config", "deep_merge"]


class ConfigError(ValueError):
    pass


def deep_merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _parse_env_value(raw: str) -> Any:
    try:
        return tomllib.loads("v = " + raw)["v"]
    except tomllib.TOMLDecodeError:
        return raw


def _env_overrides(prefix: str = "CS2MMT__") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, raw in os.environ.items():
        if not name.startswith(prefix):
            continue
        keys = [k.lower() for k in name[len(prefix):].split("__") if k]
        if not keys:
            continue
        node = out
        for k in keys[:-1]:
            node = node.setdefault(k, {})
            if not isinstance(node, dict):
                break
        else:
            node[keys[-1]] = _parse_env_value(raw)
    return out


class Config:
    def __init__(self, data: Dict[str, Any], path: Optional[Path] = None):
        self.data = data
        self.path = path
        self.base_dir = path.parent if path else Path.cwd()

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> Any:
        value = self.get(dotted)
        if value is None or value == "":
            where = " in %s" % self.path if self.path else ""
            raise ConfigError("missing setting '%s'%s" % (dotted, where))
        return value

    def section(self, dotted: str) -> Dict[str, Any]:
        value = self.get(dotted, {})
        if not isinstance(value, dict):
            raise ConfigError("'%s' should be a table" % dotted)
        return value

    def get_path(self, dotted: str, default: Optional[str] = None) -> Optional[Path]:
        """A path setting, with ~ and %VARS% expanded, relative to the config file's folder."""
        value = self.get(dotted, default)
        if value is None or value == "":
            return None
        return resolve_path(str(value), self.base_dir)

    def get_list(self, dotted: str, default: Optional[Iterable[Any]] = None) -> List[Any]:
        value = self.get(dotted)
        if value is None:
            return list(default or [])
        if isinstance(value, list):
            return value
        return [value]

    def secrets(self) -> List[str]:
        """Every value under [secrets], for redaction in logs and messages."""
        found: List[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for v in node.values():
                    walk(v)
            elif isinstance(node, str) and len(node) >= 6:
                found.append(node)

        walk(self.data.get("secrets", {}))
        return found


def resolve_path(value: str, base_dir: Path) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    p = Path(expanded)
    if not p.is_absolute():
        p = base_dir / p
    return p


def load_config(path: Path, secrets_path: Optional[Path] = None, env: bool = True) -> Config:
    path = Path(path)
    if not path.is_file():
        raise ConfigError("config file not found: %s" % path)
    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError("%s: %s" % (path, e)) from e
    sp = secrets_path
    if sp is None:
        named = data.get("secrets_file")
        sp = resolve_path(str(named), path.parent) if named else path.parent / "secrets.toml"
    if sp and Path(sp).is_file():
        with open(sp, "rb") as f:
            try:
                data = deep_merge(data, tomllib.load(f))
            except tomllib.TOMLDecodeError as e:
                raise ConfigError("%s: %s" % (sp, e)) from e
    if env:
        data = deep_merge(data, _env_overrides())
    return Config(data, path)
