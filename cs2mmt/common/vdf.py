"""Reader for Valve's text KeyValues format (VDF).

Used for Steam's ``appmanifest_<appid>.acf`` and ``libraryfolders.vdf`` files and for the
``app_info_print`` output of SteamCMD. Only reading is needed.

The format is a tree of quoted (or bare) keys, each followed by either a string value or a
``{ ... }`` block. ``//`` starts a comment and ``[$WIN32]``-style conditionals after a value are
ignored. Duplicate keys keep the last value, as Steam does.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Tuple

__all__ = ["VdfError", "loads", "find_block", "get_ci"]


class VdfError(ValueError):
    pass


_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}


def _tokens(text: str) -> Iterator[Tuple[str, str, int]]:
    """Yields (kind, value, offset) with kind in {"str", "open", "close"}."""
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n﻿":
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if c == "{":
            yield "open", c, i
            i += 1
            continue
        if c == "}":
            yield "close", c, i
            i += 1
            continue
        if c == "[":
            # Conditional such as [$WIN32] or [!$X360]; it applies to the previous pair. Skipped.
            j = text.find("]", i)
            if j < 0:
                raise VdfError("unterminated conditional at offset %d" % i)
            i = j + 1
            continue
        if c == '"':
            start = i
            i += 1
            out: List[str] = []
            while True:
                if i >= n:
                    raise VdfError("unterminated string starting at offset %d" % start)
                ch = text[i]
                if ch == "\\" and i + 1 < n and text[i + 1] in _ESCAPES:
                    out.append(_ESCAPES[text[i + 1]])
                    i += 2
                    continue
                if ch == '"':
                    i += 1
                    break
                out.append(ch)
                i += 1
            yield "str", "".join(out), start
            continue
        # Bare token: up to whitespace or a structural character.
        start = i
        while i < n and text[i] not in ' \t\r\n{}"':
            i += 1
        yield "str", text[start:i], start


def loads(text: str) -> Dict[str, Any]:
    """Parses VDF text into nested dicts of strings."""
    root: Dict[str, Any] = {}
    stack: List[Dict[str, Any]] = [root]
    pending_key: Optional[str] = None
    for kind, value, offset in _tokens(text):
        if kind == "str":
            if pending_key is None:
                pending_key = value
            else:
                stack[-1][pending_key] = value
                pending_key = None
        elif kind == "open":
            if pending_key is None:
                raise VdfError("block without a key at offset %d" % offset)
            block: Dict[str, Any] = {}
            stack[-1][pending_key] = block
            stack.append(block)
            pending_key = None
        else:  # close
            if pending_key is not None:
                raise VdfError("key %r without a value at offset %d" % (pending_key, offset))
            if len(stack) == 1:
                raise VdfError("unbalanced '}' at offset %d" % offset)
            stack.pop()
    if pending_key is not None:
        raise VdfError("key %r without a value at end of input" % pending_key)
    if len(stack) != 1:
        raise VdfError("missing '}' at end of input")
    return root


def get_ci(d: Dict[str, Any], key: str, default: Any = None) -> Any:
    """Case-insensitive lookup (Steam is inconsistent about key case, e.g. 'StateFlags')."""
    if key in d:
        return d[key]
    low = key.lower()
    for k, v in d.items():
        if k.lower() == low:
            return v
    return default


def find_block(text: str, key: str) -> Optional[Dict[str, Any]]:
    """Finds the first block named ``key`` in text that may contain non-VDF lines around it.

    SteamCMD prints log lines before and after the ``"949230" { ... }`` block of
    ``app_info_print``; this locates the block and parses only that part.
    """
    needle = '"%s"' % key
    start = 0
    while True:
        at = text.find(needle, start)
        if at < 0:
            return None
        brace = text.find("{", at + len(needle))
        between = text[at + len(needle):brace] if brace >= 0 else ""
        if brace >= 0 and between.strip() == "":
            depth = 0
            i = brace
            in_str = False
            while i < len(text):
                ch = text[i]
                if in_str:
                    if ch == "\\":
                        i += 2
                        continue
                    if ch == '"':
                        in_str = False
                elif ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        parsed = loads(text[at:i + 1])
                        return get_ci(parsed, key)
                i += 1
            return None
        start = at + len(needle)
