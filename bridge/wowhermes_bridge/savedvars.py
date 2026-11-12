"""A small, safe parser for WoW's SavedVariables Lua serialisation (PROTOCOL A.5).

Reload-mode outbound reads ``WTF\\Account\\<ACC>\\SavedVariables\\WoWHermes.lua``. That file is
written by the *game*, so it is trusted-ish, but we never ``exec`` Lua: we tokenise and parse
the subset the client emits — ``Name = value`` assignments whose values are tables, strings
(with ``\\"``, ``\\\\``, ``\\n`` and decimal escapes), numbers, booleans and nil.

The subset is deliberately strict: anything we do not understand raises ``LuaParseError``
instead of guessing, so a corrupted or truncated file is reported rather than half-applied.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

__all__ = ["LuaParseError", "parse", "parse_file", "outbox_entries"]


class LuaParseError(ValueError):
    """The SavedVariables text is not the subset we can read."""


_NUMBER = re.compile(r"-?(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    '"': '"',
    "'": "'",
    "\n": "\n",
}


class _Lexer:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.length = len(text)

    # -- helpers -----------------------------------------------------------
    def error(self, message: str) -> LuaParseError:
        line = self.text.count("\n", 0, self.pos) + 1
        return LuaParseError(f"{message} at line {line} (offset {self.pos})")

    def skip(self) -> None:
        text = self.text
        while self.pos < self.length:
            ch = text[self.pos]
            if ch in " \t\r\n\f\v,;":
                self.pos += 1
                continue
            if text.startswith("--", self.pos):
                # long comment --[[ ... ]] / --[==[ ... ]==]
                long_open = re.match(r"--\[(=*)\[", text[self.pos :])
                if long_open:
                    close = "]" + long_open.group(1) + "]"
                    end = text.find(close, self.pos + long_open.end())
                    if end < 0:
                        raise self.error("unterminated long comment")
                    self.pos = end + len(close)
                else:
                    end = text.find("\n", self.pos)
                    self.pos = self.length if end < 0 else end + 1
                continue
            return

    def peek(self) -> str:
        self.skip()
        return self.text[self.pos] if self.pos < self.length else ""

    def take(self, expected: str | None = None) -> str:
        self.skip()
        if self.pos >= self.length:
            raise self.error("unexpected end of file")
        ch = self.text[self.pos]
        if expected is not None and ch != expected:
            raise self.error(f"expected {expected!r} but found {ch!r}")
        self.pos += 1
        return ch

    # -- tokens ------------------------------------------------------------
    def name(self) -> str:
        self.skip()
        match = _NAME.match(self.text, self.pos)
        if not match:
            raise self.error("expected a name")
        self.pos = match.end()
        return match.group(0)

    def number(self) -> int | float:
        self.skip()
        match = _NUMBER.match(self.text, self.pos)
        if not match:
            raise self.error("expected a number")
        self.pos = match.end()
        raw = match.group(0)
        if raw.lower().startswith(("0x", "-0x")):
            return int(raw, 16)
        if any(c in raw for c in ".eE"):
            return float(raw)
        return int(raw)

    def string(self) -> str:
        self.skip()
        text = self.text
        if text.startswith("[[", self.pos) or re.match(r"\[=*\[", text[self.pos :]):
            long_open = re.match(r"\[(=*)\[", text[self.pos :])
            assert long_open
            close = "]" + long_open.group(1) + "]"
            start = self.pos + long_open.end()
            if text.startswith("\r\n", start):
                start += 2
            elif text.startswith("\n", start):
                start += 1
            end = text.find(close, start)
            if end < 0:
                raise self.error("unterminated long string")
            self.pos = end + len(close)
            return text[start:end]

        quote = self.take()
        if quote not in "\"'":
            raise self.error(f"expected a string, found {quote!r}")
        out: list[str] = []
        while True:
            if self.pos >= self.length:
                raise self.error("unterminated string")
            ch = text[self.pos]
            self.pos += 1
            if ch == quote:
                break
            if ch != "\\":
                out.append(ch)
                continue
            if self.pos >= self.length:
                raise self.error("unterminated escape")
            esc = text[self.pos]
            self.pos += 1
            if esc in _ESCAPES:
                out.append(_ESCAPES[esc])
            elif esc == "x":
                hexdigits = text[self.pos : self.pos + 2]
                if not re.fullmatch(r"[0-9a-fA-F]{2}", hexdigits):
                    raise self.error("bad \\x escape")
                out.append(chr(int(hexdigits, 16)))
                self.pos += 2
            elif esc.isdigit():
                digits = esc
                while len(digits) < 3 and self.pos < self.length and text[self.pos].isdigit():
                    digits += text[self.pos]
                    self.pos += 1
                code = int(digits)
                if code > 255:
                    raise self.error(f"decimal escape out of range: \\{digits}")
                # Lua 5.1 decimal escapes are raw *bytes*; the client writes UTF-8 as a run
                # of them, so rebuild bytes and decode at the end (see _join_bytes).
                out.append(chr(code))
            elif esc == "z":
                while self.pos < self.length and text[self.pos] in " \t\r\n":
                    self.pos += 1
            else:
                raise self.error(f"unknown escape \\{esc}")
        return _decode_bytes("".join(out))


def _decode_bytes(value: str) -> str:
    """Turn a run of ``\\ddd`` byte escapes (latin-1 chars 0..255) back into UTF-8 text."""
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


class _Parser:
    def __init__(self, text: str):
        self.lex = _Lexer(text)

    def chunk(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        while self.lex.peek():
            if self.lex.peek() == ";":
                self.lex.take(";")
                continue
            name = self.lex.name()
            self.lex.take("=")
            values[name] = self.value()
        return values

    def value(self) -> Any:
        ch = self.lex.peek()
        if ch == "{":
            return self.table()
        if ch in "\"'[":
            return self.lex.string()
        if ch == "-" or ch.isdigit():
            return self.lex.number()
        name = self.lex.name()
        if name == "true":
            return True
        if name == "false":
            return False
        if name == "nil":
            return None
        # Bare identifiers as values should not appear in SavedVariables; refuse rather
        # than invent a meaning.
        raise self.lex.error(f"unsupported value {name!r}")

    def table(self) -> Any:
        self.lex.take("{")
        array: list[Any] = []
        mapping: dict[Any, Any] = {}
        next_index = 1
        while True:
            ch = self.lex.peek()
            if ch == "}":
                self.lex.take("}")
                break
            if not ch:
                raise self.lex.error("unterminated table")
            if ch == "[":
                self.lex.take("[")
                key = self.value()
                self.lex.take("]")
                self.lex.take("=")
                value = self.value()
                if key is not None and value is not None:
                    mapping[key] = value
                continue
            # NAME = value  vs  positional value
            save = self.lex.pos
            try:
                name = self.lex.name()
                if self.lex.peek() == "=":
                    self.lex.take("=")
                    value = self.value()
                    if value is not None:
                        mapping[name] = value
                    continue
                self.lex.pos = save
            except LuaParseError:
                self.lex.pos = save
            value = self.value()
            if value is not None:
                array.append(value)
                next_index += 1
            else:
                next_index += 1
        if array and mapping:
            for index, item in enumerate(array, start=1):
                mapping.setdefault(index, item)
            return mapping
        if mapping:
            return mapping
        return array


def parse(text: str) -> dict[str, Any]:
    """Parse a SavedVariables file body into ``{global_name: value}``."""
    return _Parser(text).chunk()


def parse_file(path: str | Path) -> dict[str, Any]:
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return parse(text)


def outbox_entries(db: dict[str, Any], *, ack: int = 0) -> list[dict[str, Any]]:
    """``WoWHermesDB.outbox`` entries with ``id > ack``, in id order (PROTOCOL A.5)."""
    outbox = db.get("outbox") or []
    if isinstance(outbox, dict):
        outbox = [outbox[k] for k in sorted(outbox) if isinstance(outbox[k], dict)]
    entries = [
        entry
        for entry in outbox
        if isinstance(entry, dict) and isinstance(entry.get("id"), (int, float)) and int(entry["id"]) > ack
    ]
    return sorted(entries, key=lambda e: int(e["id"]))
