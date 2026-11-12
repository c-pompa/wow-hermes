"""Reference implementation of the WoWHermes wire protocol (v1).

This module is the *executable* half of docs/PROTOCOL.md. It is pure stdlib and is the
source of truth for protocol/vectors.json, which the Lua addon tests (busted) and the
Python bridge tests (pytest) both assert against. Change the spec, this file and the
vectors together, never one alone.

Layers:
  1. record   -- the payload text: fields joined by US (0x1F), records by RS (0x1E)
  2. frame    -- magic + version + id + len + payload + fletcher16
  3. cells    -- the frame as a stream of 3-bit cell values (MSB-first), 0..7
  4. slot lua -- the inbound Lua file the bridge writes for the addon to load
"""

from __future__ import annotations

import json
import math
from typing import Iterable

MAGIC = (0xC7, 0x1A)
VERSION = 1
MAX_PAYLOAD = 3200
US = "\x1f"  # field separator
RS = "\x1e"  # record separator
CELLS_PER_ROW = 200
MAX_ROWS = 48
CELL_PX = 4

# cell value v (0..7) -> (r, g, b) each 0 or 255; bit2=R bit1=G bit0=B
def cell_rgb(v: int) -> tuple[int, int, int]:
    return (255 if v & 4 else 0, 255 if v & 2 else 0, 255 if v & 1 else 0)


def rgb_cell(r: int, g: int, b: int, lo: int = 48, hi: int = 207) -> int | None:
    """Classify a sampled pixel back to a cell value; None if any channel is ambiguous."""
    out = 0
    for bit, ch in ((4, r), (2, g), (1, b)):
        if ch >= hi:
            out |= bit
        elif ch > lo:
            return None
    return out


# ---------------------------------------------------------------- records

def sanitize(text: str) -> str:
    """User text may not contain the separators; replace them with spaces."""
    return text.replace(US, " ").replace(RS, " ")


def encode_record(session: str, chat: str, msg_id: int, kind: str, flags: str, text: str) -> bytes:
    fields = [session, chat, str(msg_id), kind, flags, sanitize(text)]
    for f in fields[:5]:
        if US in f or RS in f:
            raise ValueError("separator in header field")
    return US.join(fields).encode("utf-8")


def decode_record(payload: bytes) -> dict:
    parts = payload.decode("utf-8").split(US, 5)
    if len(parts) != 6:
        raise ValueError(f"record has {len(parts)} fields, want 6")
    session, chat, msg_id, kind, flags, text = parts
    return {"session": session, "chat": chat, "id": int(msg_id), "kind": kind,
            "flags": flags, "text": text}


def encode_records(records: list[tuple]) -> bytes:
    """Several records in one frame, joined by RS (e.g. a context 'k' record + an 'm')."""
    return RS.encode().join(encode_record(*r) for r in records)


def decode_records(payload: bytes) -> list[dict]:
    return [decode_record(p) for p in payload.split(RS.encode())]


def parse_kv(text: str) -> dict:
    """Hello ('h') text is 'k=v;k=v'. Values may not contain ';'."""
    out = {}
    for item in text.split(";"):
        if item:
            k, _, v = item.partition("=")
            out[k] = v
    return out


# ---------------------------------------------------------------- frame

def fletcher16(data: Iterable[int]) -> tuple[int, int]:
    s1 = s2 = 0
    for b in data:
        s1 = (s1 + b) % 255
        s2 = (s2 + s1) % 255
    return s1, s2


def encode_frame(frame_id: int, payload: bytes) -> bytes:
    if not 1 <= len(payload) <= MAX_PAYLOAD:
        raise ValueError(f"payload length {len(payload)} outside 1..{MAX_PAYLOAD}")
    if not 0 <= frame_id <= 0xFFFF:
        raise ValueError("frame id must be uint16")
    body = bytes([VERSION, frame_id >> 8, frame_id & 0xFF, len(payload) >> 8, len(payload) & 0xFF]) + payload
    s1, s2 = fletcher16(body)
    return bytes(MAGIC) + body + bytes([s1, s2])


def decode_frame(data: bytes) -> tuple[int, bytes]:
    """Inverse of encode_frame. `data` may carry trailing padding bytes. Raises ValueError."""
    if len(data) < 10:
        raise ValueError("short frame")
    if data[0] != MAGIC[0] or data[1] != MAGIC[1]:
        raise ValueError("bad magic")
    if data[2] != VERSION:
        raise ValueError(f"unsupported version {data[2]}")
    frame_id = (data[3] << 8) | data[4]
    length = (data[5] << 8) | data[6]
    if not 1 <= length <= MAX_PAYLOAD:
        raise ValueError("bad length")
    end = 7 + length
    if len(data) < end + 2:
        raise ValueError("truncated frame")
    s1, s2 = fletcher16(data[2:end])
    if (s1, s2) != (data[end], data[end + 1]):
        raise ValueError("checksum mismatch")
    return frame_id, bytes(data[7:end])


# ---------------------------------------------------------------- cells

def cell_count(payload_len: int) -> int:
    return math.ceil(8 * (9 + payload_len) / 3)


def row_count(payload_len: int) -> int:
    return math.ceil(cell_count(payload_len) / CELLS_PER_ROW)


def bytes_to_cells(data: bytes) -> list[int]:
    """MSB-first 3-bit groups; the final group is zero-padded on the right."""
    bits = "".join(f"{b:08b}" for b in data)
    pad = (-len(bits)) % 3
    bits += "0" * pad
    return [int(bits[i:i + 3], 2) for i in range(0, len(bits), 3)]


def cells_to_bytes(cells: list[int]) -> bytes:
    bits = "".join(f"{c & 7:03b}" for c in cells)
    n = len(bits) // 8
    return bytes(int(bits[i * 8:i * 8 + 8], 2) for i in range(n))


def cell_xy(i: int) -> tuple[int, int]:
    """Top-left pixel offset of cell i inside the strip (x right, y down), at pitch 1."""
    return ((i % CELLS_PER_ROW) * CELL_PX, (i // CELLS_PER_ROW) * CELL_PX)


# ---------------------------------------------------------------- slot lua

def lua_string(s: str) -> str:
    """Lua 5.1 double-quoted literal; every byte outside printable ASCII (and \\ and ") is a
    decimal escape, so the file is pure ASCII and any UTF-8 round-trips byte-exact."""
    out = ['"']
    for b in s.encode("utf-8"):
        if b in (0x22, 0x5C) or b < 0x20 or b > 0x7E:
            out.append("\\%d" % b)
        else:
            out.append(chr(b))
    out.append('"')
    return "".join(out)


def _lua_value(v) -> str:
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return lua_string(v)
    if isinstance(v, (list, tuple)):
        return "{" + ", ".join(_lua_value(x) for x in v) + "}"
    if isinstance(v, dict):
        items = []
        for k in sorted(v):
            if not k.isidentifier():
                raise ValueError(f"non-identifier key {k!r}")
            items.append(f"{k} = {_lua_value(v[k])}")
        return "{" + ", ".join(items) + "}"
    raise TypeError(type(v))


def render_slot(state: dict, global_name: str = "WoWHermes_Slot") -> str:
    """The exact text the bridge writes into WoWHermes_Sxxx/Slot.lua and WoWHermes/Inbox.lua."""
    return f"{global_name} = {_lua_value(state)}\n"


# ---------------------------------------------------------------- vectors

def build_vectors() -> dict:
    cases = []
    samples = [
        ("hello", "a1b2c3d4", "c0", 1, "h", "",
         "v=1;b=1.60.1.70862;i=16001;char=Thrall-Area52;p=256;s=A;u=0.71;g=2560x1440;n=0"),
        ("message_ascii", "a1b2c3d4", "c0", 2, "m", "q=1", "what should I do next in Westfall?"),
        ("message_utf8", "a1b2c3d4", "c0", 3, "m", "", "naïve café — ¿dónde está el banco? 🐉"),
        ("context_and_message", "a1b2c3d4", "c0", 4, None, None, [
            ("a1b2c3d4", "c0", 4, "k", "", "zone=Elwynn Forest;sub=Goldshire;x=42.1;y=65.3;lvl=7;money=1234"),
            ("a1b2c3d4", "c0", 4, "m", "q=17", "how far to Stormwind?")]),
        ("id_wrap", "ffffffff", "c0", 65535, "m", "", "last id before wrap"),
        ("bye", "a1b2c3d4", "c0", 5, "x", "", ""),
    ]
    for name, session, chat, mid, kind, flags, text in samples:
        if kind is None:
            payload = encode_records(text)
            records = [dict(zip(("session", "chat", "id", "kind", "flags", "text"), r)) for r in text]
        else:
            payload = encode_record(session, chat, mid, kind, flags, text)
            records = [{"session": session, "chat": chat, "id": mid, "kind": kind,
                        "flags": flags, "text": sanitize(text)}]
        assert decode_records(payload) == records, name
        frame = encode_frame(mid, payload)
        cells = bytes_to_cells(frame)
        assert len(cells) == cell_count(len(payload)), name
        assert decode_frame(cells_to_bytes(cells)) == (mid, payload), name
        cases.append({
            "name": name,
            "records": records,
            "payload_hex": payload.hex(),
            "frame_hex": frame.hex(),
            "cells": "".join(str(c) for c in cells),
            "cell_count": len(cells),
            "rows": row_count(len(payload)),
        })
    fl = [
        {"data_hex": b"abcde".hex(), "s1": fletcher16(b"abcde")[0], "s2": fletcher16(b"abcde")[1]},
        {"data_hex": b"abcdef".hex(), "s1": fletcher16(b"abcdef")[0], "s2": fletcher16(b"abcdef")[1]},
    ]
    slot_state = {
        "v": 1, "seq": 42, "now": 1762640000, "ts": 1762640000.5, "ack": 17, "bridge": "up",
        "char": "Thrall-Area52",
        "replies": [
            {"chat": "c0", "id": 17, "status": "done", "t": 1762639998,
             "text": 'Quote " backslash \\ ]] --[[ newline\nemoji 🐉 tab\t end'},
            {"chat": "c0", "id": 18, "status": "working", "t": 1762639999, "text": "thinking…"},
        ],
    }
    return {
        "protocol_version": VERSION,
        "magic_hex": bytes(MAGIC).hex(),
        "frames": cases,
        "fletcher16": fl,
        "rgb": [{"v": v, "rgb": list(cell_rgb(v))} for v in range(8)],
        "slot": {"state": slot_state, "lua": render_slot(slot_state)},
    }


if __name__ == "__main__":  # python3 protocol/wowhermes_protocol.py > protocol/vectors.json
    print(json.dumps(build_vectors(), indent=2, ensure_ascii=False))
