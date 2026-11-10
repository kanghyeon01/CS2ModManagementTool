"""``tomllib`` on Python 3.11+, the bundled reader on 3.9 and 3.10 (Synology's Python package)."""

from __future__ import annotations

try:  # Python 3.11+
    import tomllib as _impl
    from tomllib import TOMLDecodeError
except ModuleNotFoundError:  # pragma: no cover - exercised on old Pythons only
    from . import minitoml as _impl
    from .minitoml import TOMLDecodeError

__all__ = ["load", "loads", "TOMLDecodeError"]


def loads(text: str):
    return _impl.loads(text)


def load(fp):
    return _impl.load(fp)
