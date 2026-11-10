"""A small TOML reader for Python 3.9 and 3.10, which have no ``tomllib``.

Synology DSM 7.2 ships Python 3.8 and offers 3.9 as a package, so the controller must run without
``tomllib`` there. This covers what the tool's config files use: tables, arrays of tables, dotted
and quoted keys, all four string kinds, integers, floats, booleans, arrays and inline tables.
Dates and times are not supported (the tool never uses them). On Python 3.11+ the standard
``tomllib`` is used instead (see ``toml_compat``).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

__all__ = ["TOMLDecodeError", "loads", "load"]


class TOMLDecodeError(ValueError):
    pass


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_ESC = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\", "e": "\x1b"}
_NUMBER = re.compile(
    r"[+-]?(?:0x[0-9A-Fa-f_]+|0o[0-7_]+|0b[01_]+|inf|nan|"
    r"(?:\d[\d_]*)(?:\.\d[\d_]*)?(?:[eE][+-]?\d[\d_]*)?)"
)


class _Parser:
    def __init__(self, text: str):
        self.s = text.replace("\r\n", "\n")
        self.i = 0
        self.n = len(self.s)
        self.root: Dict[str, Any] = {}
        self.defined_tables: set = set()

    # -- low level --
    def error(self, msg: str) -> TOMLDecodeError:
        line = self.s.count("\n", 0, self.i) + 1
        col = self.i - (self.s.rfind("\n", 0, self.i) + 1) + 1
        return TOMLDecodeError("%s (line %d, column %d)" % (msg, line, col))

    def peek(self, k: int = 0) -> str:
        j = self.i + k
        return self.s[j] if j < self.n else ""

    def skip_ws(self) -> None:
        while self.i < self.n and self.s[self.i] in " \t":
            self.i += 1

    def skip_comment(self) -> None:
        if self.peek() == "#":
            while self.i < self.n and self.s[self.i] != "\n":
                self.i += 1

    def skip_ws_nl_comments(self) -> None:
        while self.i < self.n:
            c = self.s[self.i]
            if c in " \t\n":
                self.i += 1
            elif c == "#":
                self.skip_comment()
            else:
                break

    def expect_eol(self) -> None:
        self.skip_ws()
        self.skip_comment()
        if self.i < self.n:
            if self.s[self.i] != "\n":
                raise self.error("expected the end of the line")
            self.i += 1

    # -- keys --
    def parse_key(self) -> List[str]:
        parts: List[str] = []
        while True:
            self.skip_ws()
            c = self.peek()
            if c == '"':
                parts.append(self.parse_basic_string())
            elif c == "'":
                parts.append(self.parse_literal_string())
            else:
                m = _BARE_KEY.match(self.s, self.i)
                if not m:
                    raise self.error("invalid key")
                parts.append(m.group(0))
                self.i = m.end()
            self.skip_ws()
            if self.peek() == ".":
                self.i += 1
                continue
            return parts

    # -- strings --
    def parse_basic_string(self) -> str:
        if self.s.startswith('"""', self.i):
            return self.parse_ml_basic()
        self.i += 1
        out: List[str] = []
        while True:
            if self.i >= self.n:
                raise self.error("unterminated string")
            c = self.s[self.i]
            if c == '"':
                self.i += 1
                return "".join(out)
            if c == "\n":
                raise self.error("newline in a single-line string")
            if c == "\\":
                out.append(self.parse_escape())
                continue
            out.append(c)
            self.i += 1

    def parse_escape(self) -> str:
        self.i += 1
        c = self.peek()
        if c in _ESC:
            self.i += 1
            return _ESC[c]
        if c in ("u", "U"):
            width = 4 if c == "u" else 8
            hexpart = self.s[self.i + 1:self.i + 1 + width]
            if len(hexpart) != width or not re.fullmatch(r"[0-9A-Fa-f]+", hexpart):
                raise self.error("bad unicode escape")
            self.i += 1 + width
            return chr(int(hexpart, 16))
        raise self.error("bad escape \\%s" % c)

    def parse_ml_basic(self) -> str:
        self.i += 3
        if self.peek() == "\n":
            self.i += 1
        out: List[str] = []
        while True:
            if self.i >= self.n:
                raise self.error("unterminated multi-line string")
            if self.s.startswith('"""', self.i):
                # Up to two quotes may sit right before the closing delimiter.
                extra = 0
                while self.s.startswith('"', self.i + 3 + extra) and extra < 2:
                    extra += 1
                out.append('"' * extra)
                self.i += 3 + extra
                return "".join(out)
            c = self.s[self.i]
            if c == "\\":
                nxt = self.peek(1)
                if nxt in " \t\n":
                    # Line-ending backslash: trim the newline and the following whitespace.
                    j = self.i + 1
                    while j < self.n and self.s[j] in " \t":
                        j += 1
                    if j < self.n and self.s[j] == "\n":
                        while j < self.n and self.s[j] in " \t\n":
                            j += 1
                        self.i = j
                        continue
                out.append(self.parse_escape())
                continue
            out.append(c)
            self.i += 1

    def parse_literal_string(self) -> str:
        if self.s.startswith("'''", self.i):
            self.i += 3
            if self.peek() == "\n":
                self.i += 1
            end = self.s.find("'''", self.i)
            if end < 0:
                raise self.error("unterminated multi-line literal string")
            while self.s.startswith("'", end + 3) and end + 3 < self.n:
                end += 1
            text = self.s[self.i:end]
            self.i = end + 3
            return text
        self.i += 1
        end = self.s.find("'", self.i)
        nl = self.s.find("\n", self.i)
        if end < 0 or (0 <= nl < end):
            raise self.error("unterminated literal string")
        text = self.s[self.i:end]
        self.i = end + 1
        return text

    # -- values --
    def parse_value(self) -> Any:
        self.skip_ws()
        c = self.peek()
        if c == '"':
            return self.parse_basic_string()
        if c == "'":
            return self.parse_literal_string()
        if c == "[":
            return self.parse_array()
        if c == "{":
            return self.parse_inline_table()
        if self.s.startswith("true", self.i) and not _BARE_KEY.match(self.s, self.i + 4):
            self.i += 4
            return True
        if self.s.startswith("false", self.i) and not _BARE_KEY.match(self.s, self.i + 5):
            self.i += 5
            return False
        if re.match(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}", self.s[self.i:self.i + 10]):
            raise self.error("dates and times are not supported by this reader")
        m = _NUMBER.match(self.s, self.i)
        if not m or not m.group(0):
            raise self.error("invalid value")
        text = m.group(0)
        self.i = m.end()
        return self.number(text)

    def number(self, text: str) -> Any:
        t = text.replace("_", "")
        sign = -1 if t.startswith("-") else 1
        body = t.lstrip("+-")
        try:
            if body.startswith("0x"):
                return sign * int(body[2:], 16)
            if body.startswith("0o"):
                return sign * int(body[2:], 8)
            if body.startswith("0b"):
                return sign * int(body[2:], 2)
            if body in ("inf", "nan"):
                return float(t)
            if re.fullmatch(r"\d+", body):
                if len(body) > 1 and body.startswith("0"):
                    raise self.error("leading zeros are not allowed")
                return sign * int(body)
            return float(t)
        except ValueError:
            raise self.error("invalid number %r" % text)

    def parse_array(self) -> List[Any]:
        self.i += 1
        out: List[Any] = []
        while True:
            self.skip_ws_nl_comments()
            if self.peek() == "]":
                self.i += 1
                return out
            out.append(self.parse_value())
            self.skip_ws_nl_comments()
            c = self.peek()
            if c == ",":
                self.i += 1
                continue
            if c == "]":
                self.i += 1
                return out
            raise self.error("expected ',' or ']' in an array")

    def parse_inline_table(self) -> Dict[str, Any]:
        self.i += 1
        table: Dict[str, Any] = {}
        self.skip_ws()
        if self.peek() == "}":
            self.i += 1
            return table
        while True:
            key = self.parse_key()
            self.skip_ws()
            if self.peek() != "=":
                raise self.error("expected '=' in an inline table")
            self.i += 1
            value = self.parse_value()
            self.assign(table, key, value)
            self.skip_ws()
            c = self.peek()
            if c == ",":
                self.i += 1
                continue
            if c == "}":
                self.i += 1
                return table
            raise self.error("expected ',' or '}' in an inline table")

    # -- structure --
    def assign(self, table: Dict[str, Any], key: List[str], value: Any) -> None:
        node = table
        for part in key[:-1]:
            nxt = node.setdefault(part, {})
            if not isinstance(nxt, dict):
                raise self.error("key %s is already a value" % ".".join(key))
            node = nxt
        if key[-1] in node:
            raise self.error("duplicate key %s" % ".".join(key))
        node[key[-1]] = value

    def table_at(self, path: List[str], array: bool) -> Dict[str, Any]:
        node: Any = self.root
        for idx, part in enumerate(path):
            last = idx == len(path) - 1
            if last and array:
                arr = node.setdefault(part, [])
                if not isinstance(arr, list):
                    raise self.error("%s is not an array of tables" % ".".join(path))
                new: Dict[str, Any] = {}
                arr.append(new)
                return new
            nxt = node.get(part) if isinstance(node, dict) else None
            if nxt is None:
                nxt = {}
                node[part] = nxt
            if isinstance(nxt, list):
                if not nxt or not isinstance(nxt[-1], dict):
                    raise self.error("%s is not a table" % ".".join(path[: idx + 1]))
                nxt = nxt[-1]
            if not isinstance(nxt, dict):
                raise self.error("%s is not a table" % ".".join(path[: idx + 1]))
            node = nxt
        key = (tuple(path), id(node))
        if key in self.defined_tables:
            raise self.error("table [%s] defined twice" % ".".join(path))
        self.defined_tables.add(key)
        return node

    def parse(self) -> Dict[str, Any]:
        current = self.root
        while True:
            self.skip_ws_nl_comments()
            if self.i >= self.n:
                return self.root
            if self.s.startswith("[[", self.i):
                self.i += 2
                path = self.parse_key()
                if not self.s.startswith("]]", self.i):
                    raise self.error("expected ']]'")
                self.i += 2
                current = self.table_at(path, array=True)
                self.expect_eol()
                continue
            if self.peek() == "[":
                self.i += 1
                path = self.parse_key()
                if self.peek() != "]":
                    raise self.error("expected ']'")
                self.i += 1
                current = self.table_at(path, array=False)
                self.expect_eol()
                continue
            key = self.parse_key()
            self.skip_ws()
            if self.peek() != "=":
                raise self.error("expected '='")
            self.i += 1
            value = self.parse_value()
            self.assign(current, key, value)
            self.expect_eol()


def loads(text: str) -> Dict[str, Any]:
    return _Parser(text).parse()


def load(fp: Any) -> Dict[str, Any]:
    data = fp.read()
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    return loads(data)
