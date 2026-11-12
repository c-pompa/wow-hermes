# wowhermes-bridge — Windows companion for the WoWHermes addon

The bridge is the only process outside the game. It reads the addon's pixel strip with a
screen/window capture API, decodes frames, forwards them to the Hermes gateway over HTTP,
and writes replies back into pre-created load-on-demand addon folders plus `.wav` doorbells.
It never touches the game process, its memory, or its input (see `docs/PROTOCOL.md` §E).

```
uv run wowhermes-bridge install --wow "C:\Program Files (x86)\World of Warcraft\_forever_"
uv run wowhermes-bridge run
uv run wowhermes-bridge status
uv run wowhermes-bridge decode-image capture.png
uv run wowhermes-bridge probe-capture --out strip.png
```

Layout of the package:

| module | role |
|---|---|
| `protocol.py` | byte-identical copy of `protocol/wowhermes_protocol.py` (a test enforces it) |
| `decoder.py` | any-size RGB/BGRA crop (the bridge grabs 1600×400) → `(id, payload)` frames |
| `capture.py` | window discovery + WGC (primary) / mss-GDI (fallback) capture |
| `slots.py` | atomic slot-file publishing, doorbell pools, silent-wav bytes |
| `installer.py` | copies the addon, generates 256 slot addons, creates `ctl/`, `sig/`, `ack/` |
| `savedvars.py` | safe parser for WoW's SavedVariables Lua serialisation (reload mode) |
| `hermes_client.py` | PROTOCOL §C HTTP client + long-poll outbox thread |
| `state.py` | exchanges, transcripts, cursors, slot-state construction |
| `app.py` | capture loop, dedupe, ack, publish, forward, reply, doorbell |
| `overlay.py` | PySide6 always-on-top panel (optional extra) |
| `config.py` | `%LOCALAPPDATA%\wowhermes\config.toml` discovery and defaults |
| `cli.py` | the `wowhermes-bridge` console script |

Configuration lives in `%LOCALAPPDATA%\wowhermes\config.toml`:

```toml
wow_dir = 'C:\Program Files (x86)\World of Warcraft\_forever_'
account = "ACCOUNT"            # auto-detected from WTF\Account when omitted
gateway_url = "http://127.0.0.1:8791"  # set your Hermes host explicitly if it runs elsewhere
# token is read from WOWHERMES_TOKEN when set, else this file
# token = "…"
character = "Thrall-Area52"   # optional; learned from the hello frame otherwise
```

`WOWHERMES_TOKEN` always wins over the file so the secret does not have to be on disk.

## Notes and known constraints

- The capture search region is the top-left **1600×400** physical pixels of the client
  area (PROTOCOL A.3, rev 2026-11-08). That covers the widest strip (pitch 2.0 = 1600 px)
  and the tallest one (48 rows at pitch 2.0 = 384 px); the decoder itself is
  size-agnostic. `probe-capture --wide` still exists but is the same region.
- Slot publishing writes the rendered state to **all 256 slots** on every publish (PROTOCOL
  B.2; there is no cursor mode). The rendered file is capped at 16 KB: the latest 4
  replies, each text truncated UTF-8-safely at 3500 bytes with ` … (full reply in
  overlay)`, plus a restore bundle of at most 10 KB (newest first, ≤600 B per entry).

## Development

```bash
cd bridge
uv run pytest              # works from a clean checkout: pytest lives in [dependency-groups].dev
uv run --all-extras pytest # also installs PySide6 for the offscreen Qt test
uv run python scripts/bench_publish.py   # time a full 256-slot, 16 KB publish
```
