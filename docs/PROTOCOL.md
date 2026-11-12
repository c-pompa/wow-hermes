# WoWHermes protocol v1 — the contract between addon, bridge and Hermes plugin

**Status:** frozen for v1 (2026-11-08). Executable reference: `protocol/wowhermes_protocol.py`;
golden vectors: `protocol/vectors.json` (regenerate with
`python3 protocol/wowhermes_protocol.py > protocol/vectors.json`). The Lua addon tests and the
Python bridge tests MUST assert against `vectors.json`. Any change = spec + reference + vectors +
both test suites in one commit, bump `VERSION`.

Derived from research docs 02, 03, 05, 06 (§9). Where this file and the research differ, this file
wins.

```
 WoW: Forever client (game-pc)                     game-pc (user session)                Mac (Hermes gateway)
 ┌───────────────────────────┐   pixels (A)   ┌──────────────────────────────┐  HTTP (C)  ┌──────────────────────┐
 │ WoWHermes addon           │ ─────────────► │ wowhermes-bridge (Python)    │ ─────────► │ Hermes platform      │
 │  /h <text>, chat window   │                │  capture+decode, overlay     │ ◄───────── │ plugin "wow" (aiohttp│
 │                           │ ◄───────────── │  slot publisher, doorbells   │  long-poll │  :8791, bearer token)│
 └───────────────────────────┘ slot files (B) └──────────────────────────────┘            └──────────────────────┘
```

## A. Outbound: addon → bridge (pixel strip)

### A.1 Records (payload text)
A payload is one or more **records** joined by RS `0x1E`. A record is 6 fields joined by US `0x1F`:

```
session ␟ chat ␟ id ␟ kind ␟ flags ␟ text
```
- `session` 8 lowercase hex, minted once when `WoWHermesDB` is created (survives /reload).
- `chat` — `c0` in v1 (one conversation per character; multi-chat is v2).
- `id` — decimal, the frame id (A.2); all records in a frame share it.
- `kind`:
  | kind | meaning | text |
  |---|---|---|
  | `h` | hello, sent at PLAYER_LOGIN/after /reload and every 60 s while the bridge is unseen | `k=v;…` with keys `v` (protocol), `b` (build string from `GetBuildInfo()`), `i` (interface), `char` (`Name-Realm`), `p` (installed slot count), `s` (doorbell mode `A`/`B`/`0`), `u` (UIParent effective scale), `g` (`WxH` physical), `n` (last msg id), `cap` (v1.1 capabilities, comma list: `quest`, `cmd`) |
  | `k` | game context, only when changed; precedes an `m` in the same frame | `k=v;…`: base keys `zone`,`sub`,`x`,`y`,`lvl`,`class`,`race`,`money`,`combat`,`instance`,`lockdown`; v1.3 appends `prof`,`bags`,`xp`,`rested`,`quest` (below) |
  | `m` | user message to Hermes | UTF-8 text, separators replaced by spaces |
  | `x` | bye (PLAYER_LOGOUT) | empty |
  | `e` | Lua error report captured by the addon (dev aid) | first 900 bytes of message+stack |
  | `c` | player confirmation of a bridge `command` (Accept/Cancel in the addon's own window) | `id=<command id>;ok=1` (or `ok=0`) |
  | `q` | quest snapshot, sent with an `m` when it changed and `cap=quest` (v1.1) | `id=<questID>;title=<title>;zone=<zone>;objs=<obj1> | <obj2> | …` (objectives only: Forever has no quest-prose getter) |
- `flags` — `k=v;…`, may be empty. v1 defines **`q=<N>`** on `h` and `m` records: the slot number
  (1..256) the addon will load next (`0` = pool exhausted). The bridge uses it for the priority
  window (B.2). Unknown flag keys are ignored.
- **Dedupe is per frame, not per record:** the bridge processes every record of a frame, then marks
  `(session, id)` handled. A `k` record never consumes the id (rev 2026-11-09, e2e finding).

#### A.1.1 Context keys, base (v1.0) and v1.3 additions
Base keys keep their positions; the v1.3 keys are **appended after `lockdown`** because an adapter
that keeps only the first N fields of a record must still see a complete base record. Consumers
ignore keys they do not know, so an old bridge/adapter reading a new addon loses only the tail.

| key | type | v1.3 meaning |
|---|---|---|
| `zone`, `sub` | text | zone / subzone name (unchanged) |
| `x`, `y` | decimal, 1 dp | map position in percent (unchanged) |
| `lvl` | int | character level (unchanged) |
| `class`, `race` | text token | `UnitClass`/`UnitRace` token as the client returns it, e.g. `WARRIOR` / `Human` (unchanged) |
| `money` | int | copper on hand (unchanged) |
| `combat` | `0`/`1` | in combat (unchanged) |
| `instance` | `0`/`1` | inside an instance (unchanged) |
| `lockdown` | `0`/`1` | chat-messaging lockdown active (unchanged) |
| `prof` | text, ≤80 B | primary professions with rank: `Mining 12/75,Skinning 30/75`; comma-separated in profession-slot order (1, then 2). Omitted when no profession is learned. A rank is dropped when `rank`/`maxRank` is missing or secret. |
| `bags` | int | total free slots over the backpack (bag 0) and the equipped bags (`1..NUM_BAG_SLOTS`, plus the reagent bag when one is equipped). Empty bag slots contribute nothing. Omitted when no count is readable. |
| `xp` | int 0–100 | percent of the current level, `floor(UnitXP/UnitXPMax*100+0.5)`. Omitted at max level (`UnitXPMax` 0) or when either value is unavailable/secret. |
| `rested` | int ≥1 | rested XP as a percent of the level (`GetXPExhaustion` / `UnitXPMax`). Omitted when nothing is rested or the result rounds below 1 %. |
| `quest` | text, ≤60 B | super-tracked quest, else the first watched quest: `<questID>:<title>`, e.g. `26228:Wolves at the Door`. The title is sanitized (`;`, `=`, `|` and control bytes become spaces) and byte-cut on a UTF-8 boundary. Omitted when no quest is tracked/watchable or the title is unnameable. |

Every value is feature-detected, `pcall`-guarded and secret-guarded (`issecretvalue`): a value the
client marks secret is treated as absent, never re-sent as a placeholder. A value may not contain
`;`, `=`, `|` or a control byte; the addon replaces them with spaces before joining.

**Size:** the whole `k` text is kept at or below 350 bytes of the 3200-byte payload budget. Values
are individually capped (`prof` 80 B, `quest` 60 B); when the assembled text is still over 350 B the
addon sheds the new keys in the order `quest`, `prof`, `rested`, `bags`, `xp` and never a base key.
The existing rule still applies: if a frame would exceed 3200 bytes the context is dropped first,
then the `q` snapshot, and only then is the message refused.

**Change detection:** the addon resends `k` when it differs from the last one it framed. For that
comparison only, `xp` is rounded down to a 5-point step and `rested` to a 10-point step, so a kill
does not force a new ~200-byte record on every message; a resent record always carries the exact
current values. The level itself (`lvl`) is exact, so a ding is always a change.

### A.2 Frame (bytes)
```
0      0xC7
1      0x1A
2      version = 1
3..4   id    uint16 big-endian, per-session monotonic from 1, wraps 65535→1 (0 unused)
5..6   len   uint16 big-endian, 1..3200
7..    payload (len bytes)
+0,+1  Fletcher-16 (s1, s2), mod 255, over bytes 2 .. end of payload
```
Over-size → the addon refuses to send, keeps the draft, tells the player.

### A.3 Cells and on-screen layout
- Frame bytes → bit stream MSB-first → 3-bit groups (final group zero-padded) → cell values 0..7.
  `cells = ceil(8*(9+len)/3)`, `rows = ceil(cells/200)` (max 43 rows for 3200 B; crop budget 48).
- Cell colour: `bit2=R, bit1=G, bit0=B`, each channel 0 or 1 (`SetColorTexture(r,g,b,1)`).
- Cell `i` occupies a 4×4 *physical-pixel* square at `x=(i%200)*4, y=floor(i/200)*4` from the
  strip origin. Strip frame: parent `UIParent`, strata `TOOLTIP`, level 10000,
  `SetIgnoreParentScale(true)`, `SetScale(768 / physicalHeight)`, `TOPLEFT` at (0,0), size
  `800 × rows*4`. Textures `SetSnapToPixelGrid(false)`, `SetTexelSnappingBias(0)`.
- **Units = pixels.** With `SetIgnoreParentScale(true)` and `SetScale(768/physicalHeight)` the
  frame's effective scale equals Blizzard's pixel-to-UI factor, so **1 UI unit = 1 physical pixel**
  (`PixelUtil.GetNearestPixelSize`: `numPixels = uiUnits * layoutScale / (768/physH)`, Forever
  `Blizzard_SharedXML/PixelUtil.lua:8-28`). All sizes/offsets above are therefore passed to
  `SetSize`/`SetPoint` *as-is* — never divided or multiplied by the scale. (Rev 2026-11-08 after
  WS-A review.)
- Bridge capture search region: top-left **1600 × 400** physical px of the client area (covers
  pitch ≤ 2 for 48 rows); the decoder is size-agnostic.
- Decoder: find magic by scanning pitch hypotheses {1, 1.25, 1.333, 1.5, 1.75, 2}, sample cell
  centres, reject the frame on any ambiguous channel (`48 < c < 207`), bad magic/version/len, or
  checksum. Dedupe on `(session, id)` before acting.

### A.4 Strip lifecycle
Show strip → poll every 0.25 s for `ack/NNN.wav` (B.3) → hide on ack. No ack in 20 s → redraw
(same id), max 3 tries → mark `unsent`, tell the player. One in-flight frame at a time; the addon
queues up to 4 more. `NNN = ((id-1) % 256) + 1`, zero-padded to 3 digits.

### A.5 Reload-mode outbound (fallback, setting `transport = "reload"`)
When the strip is disabled (or capture is unavailable), the addon appends each record to
`WoWHermesDB.outbox` and shows "type /reload to send". The client writes
`WTF\Account\<ACC>\SavedVariables\WoWHermes.lua` on /reload/logout; the bridge watches that file's
mtime, parses it (WoW's own SavedVariables serialization: `WoWHermesDB = { ["k"] = v, [1] = {…}, }`,
strings with `\"`, `\\`, `\n` escapes) and processes `outbox` entries with `id > ack`. Outbox
entry shape (also the addon's history entry shape):
```lua
{ id = 17, chat = "c0", kind = "m", text = "…", t = 1762640000 }
```
`WoWHermesDB` top-level keys (v1): `v`, `session`, `nextId`, `settings`, `outbox`, `chats` (`chats.c0.history`
= list of `{role="user"|"hermes", id, t, text, status}`), `window` (position), `char` (`Name-Realm`).

## B. Inbound: bridge → addon (files)

### B.1 Layout (created by `wowhermes-bridge install`)
```
<WoW>\_forever_\Interface\AddOns\
  WoWHermes\                 the addon (copied from addon/WoWHermes)
    Inbox.lua                WoWHermes_Slot = nil  (rewritten by the bridge; read on /reload/login)
    ctl\valid.wav            124-byte silent wav, exists from install
    sig\  ack\               doorbell pools, files 001.wav..256.wav created/deleted at runtime
  WoWHermes_S001\ … WoWHermes_S256\
    WoWHermes_S001.toc       ## Interface: 16001 / ## LoadOnDemand: 1 / ## Dependencies: WoWHermes
                             ## X-WoWHermes-Slot: 1 / Slot.lua
    Slot.lua                 WoWHermes_Slot = nil
```
New addon folders are discovered at client start (and reportedly at /reload); the hello `p` field
reports how many slot addons the client actually sees (`C_AddOns.GetAddOnInfo`). The post-install
prompt therefore offers a `/reload` (`reload` key, B.2) — it succeeds for an updated addon and is
normally enough for a new folder — and only tells the player to restart the client if the addon
still does not appear, which is the Forever fallback for a folder the loader did not rescan.

### B.2 Slot file format
Exactly `protocol.render_slot(state)` — one assignment `WoWHermes_Slot = {…}`, pure ASCII, every
byte outside 0x20–0x7E plus `"` and `\` written as a decimal escape (`\226\128\166`). Keys:
```
v=1, seq=<publish counter>, boot=<publisher process start epoch float>, now=<epoch s>, ts=<epoch float>, ack=<highest consumed outbound id>,
bridge="up"|"starting"|"down", char="Name-Realm",
replies={ {chat="c0", id=<outbound id>, status="working"|"done"|"error", t=<epoch>, text=<≤3500 B>}, … }  -- ≤4 entries
restore=nil | { {role="user"|"hermes", id=…, t=…, text=…}, … }   -- only after a new session token
confirm=nil | { id="c17", action="addon.install"|"addon.uninstall"|"app.curseforge.install", name="…", t=<epoch>, exp=<epoch> }
note=nil | "…"                              -- one-shot in-game outcome line, ≤400 B
reload=nil | { name="Questie", ts=<epoch float>, exp=<epoch float>,
               enable={"Questie","QuestieDB"},             -- post-install reminder
               disable={"Questie"} }                       -- post-removal reminder
```
`confirm` asks the addon to open its own Accept/Cancel window for a pending `command`; the
player's answer returns as a `c` record. `note` is printed once. `reload` is published **only
after a successful `addon.install` or `addon.uninstall`** (never on Accept alone, never on
failure) and republished for **60 s** (`exp = ts + 60`): the addon opens an addon-owned
reminder window. For an install it reads `NAME installed. Type /reload in N seconds to load
the addon. If it does not appear afterward, restart WoW once.`; for a removal the same window
reads `NAME was removed. Type /reload in N seconds to finish removing it.` and its title is
`Hermes removed an addon`. Either way the window has **Got it** / **Cancel**. The 10-second
countdown ends at `Type /reload now`; it never reloads the UI — Forever blocks
addon-initiated reloads. Both buttons only dismiss the reminder and suppress that prompt
(`ts`) locally and in SavedVariables (`WoWHermesDB.reloadSeen`), so a repeat publish or a
`/reload` in the window does not ask twice. In combat the addon prints the manual `/reload`
line instead of opening the window. `/h reload` shows the same install reminder with the
generic name "Your addon".

`reload.enable` is the optional list of addon folders the addon passes to
`C_AddOns.EnableAddOn` (Forever: documented `AllowedWhenUntainted`, the call Blizzard's own
AddOns list makes) **before** it shows the reminder, in order, so the `/reload` it asks for loads
the addon instead of leaving it disabled. The bridge builds it from the successful install: the
installed folder first, then the TOC's required dependencies (`## Dependencies` / `## RequiredDeps`)
whose folders are actually present. Optional dependencies and absent folders are omitted; when the
TOC carries no dependency information the list is just the installed folder. A folder the client
has not discovered yet (first install of a brand-new addon) can refuse or error; that is expected,
non-fatal and never blocks the reminder. The addon then reports the outcome in chat —
`N enabled.`, or `Could not enable N yet; enable it in the AddOns list after /reload.` — and never
writes `WTF/AddOns.txt`, never uses a protected call and never reloads the UI. Because the whole
list rides inside `reload`, the existing size rules are unchanged: when the 16 KB budget is tight,
`note` and then `confirm` are the last optional keys dropped before the publish is refused;
`reload` is dropped before `confirm`.

`reload.disable` is the mirror image for a successful `addon.uninstall`: the list holds the
removed folder, and the addon passes each entry to `C_AddOns.DisableAddOn` (Forever:
`AddOnsDocumentation.lua:10`, also `AllowedWhenUntainted`) **before** it shows the removal
reminder, so `/reload` drops the addon from the AddOns list instead of leaving a stale entry.
A folder the client already forgot can refuse or error; that is expected, non-fatal and never
blocks the reminder. The addon reports `N disabled.` or `Could not disable N yet; the removed
addon may stay listed until you restart WoW.` The two lists never ride together — an install
publishes `enable`, a removal publishes `disable` — and both live inside `reload`, so the size
rules above are unchanged.

`replies` is the latest state per exchange, not a queue. The addon applies a reply when its `id`
matches a pending/known message and `seq` is newer within one publisher. A newer `boot` marks a
bridge restart and resets the addon's expected `seq`; an older `boot` is a leftover slot and is
ignored. Legacy publishers without `boot` keep the original monotonic-`seq` behavior. Writes: `tmp` → `fsync` → `os.replace`, retry WinError 5/32 with backoff.
**Every publish reaches all slots, priority first:** it writes the window `q … q+15` (from the
latest `q` flag) synchronously, then `Inbox.lua` and the rest of the pool in a background pass. A
newer publish cancels and restarts the background pass. Doorbells (`sig`) are raised only after
the priority window is on disk. (Measured: 257 files take ~765 ms on game-pc with NTFS + Defender
vs 59 ms on the Mac, so the window keeps reply latency low.) There is no cursor mode in v1. To bound cost, a rendered slot file is ≤ 16 KB: `replies`
holds the latest **4** exchanges, each `text` truncated UTF-8-safely to **3500 bytes** with the
marker ` … (full reply in overlay)`; a `restore` bundle, when present, takes ≤ 10 KB (newest
entries first, each ≤ 600 bytes) and `replies` shrinks to fit the rest. The overlay always keeps
the full text. (Rev 2026-11-08: replaces the 8 KB/16-entry/cursor-mode rule.)

The addon loads slots with `C_AddOns.LoadAddOn("WoWHermes_Sxxx")` in ascending order, each once per
UI session; it reads `WoWHermes_Slot` in its `ADDON_LOADED` handler for that slot name and sets the
global back to nil. Pool exhausted → rely on `Inbox.lua` + ask the player for `/reload`.

### B.3 Doorbells (existence signalling)
At login the addon probes `PlaySoundFile` (guard: `type(PlaySoundFile)=="function"`, call inside
`pcall`, `StopSound` any handle immediately): `ctl\absent.wav` must be falsy and `ctl\valid.wav`
truthy → mode `A` (existence). Otherwise mode `0` (no doorbells; slot polling only). Mode `B`
(content signalling) is reserved, not implemented in v1.
- `ack\NNN.wav` created by the bridge as soon as frame `id` is decoded; kept for **at least 2 s**
  and one publish generation, deleted after that or after 60 s at the latest. The addon also treats
  a loaded slot with `ack >= id` as an ack (belt and braces).
- `sig\NNN.wav` created when the reply for `id` is published (`done`/`error`); deleted when a hello
  or later message shows the addon saw it.
- Bridge sweeps both pools at startup.

### B.4 Addon poll schedule
| situation | action |
|---|---|
| idle | nothing |
| frame on strip | check `ack\NNN` every 0.25 s |
| reply pending, doorbell mode A | check `sig\NNN` every 0.5 s → load next slot when it rings; safety slot load at +30 s, +120 s |
| reply pending, mode 0 | load a slot at +2, +5, +10, +20, +40, +80 s, then every 120 s; give up at 10 min |

## C. Bridge ↔ Hermes plugin (HTTP, JSON)

Base URL `http://<gateway-host>:8791` (Mac LAN `192.0.2.10`). Every request except loopback
`/v1/wow/health` carries `Authorization: Bearer <WOW_BRIDGE_TOKEN>`; compare with
`hmac.compare_digest`. Bodies ≤ 64 KiB.

- `GET /v1/wow/health` → `{"ok":true,"platform":"wow","version":1,…}` — bearer optional from
  loopback, required otherwise; a supplied bearer is always validated.
- Auth failures: missing/bad bearer → **401** (bridge: configuration error, stop and report).
  Allowlist rejection → `202 accepted:false reason:"unauthorized"`.
- `POST /v1/wow/message`
  ```json
  {"character":"Thrall-Area52","chat":"c0","msg_id":"a1b2c3d4:17","text":"…","ts":1762640000.1,
   "context":{"zone":"Elwynn Forest","lvl":"7"}, "client":{"build":"1.60.1.70862","interface":16001}}
  ```
  → `202 {"ok":true,"accepted":true}` or `202 {"ok":true,"accepted":false,"reason":"unauthorized"|"duplicate"}`.
  The plugin builds a `MessageEvent` with `chat_id = user_id = character`, `chat_type="dm"`,
  `message_id = msg_id`; game context is prepended to the text as a single bracketed line
  `[WoW context: zone=…; lvl=…]` only when present.
- `GET /v1/wow/outbox?character=<c>&since=<seq>&wait=<0..25>` → long-poll:
  ```json
  {"character":"Thrall-Area52","next":44,"messages":[
    {"seq":42,"kind":"typing","ts":…},
    {"seq":43,"kind":"reply","text":"…","reply_to":"a1b2c3d4:17","ts":…},
    {"seq":44,"kind":"done","reply_to":"a1b2c3d4:17","ts":…}]}
  ```
  `kind ∈ reply|typing|done|notice|command` (no `final` flag). `command` (v1.1) is an
  **allowlisted** bridge action: `{"seq":N,"kind":"command","id":"c7","action":"open.url","url":"https://… or curseforge://install?addonId=…&fileId=…"}`.
  `open.url`: the bridge validates scheme/host allowlists (`https:` any host,
  `curseforge://install` only) and opens it with `os.startfile`. No paths, no shell.
  `addon.install`: `{projectId, fileId, name, version, downloadUrl, apiKey, confirm:"player"}` —
  download only from `edge.forgecdn.net` / `mediafilez.forgecdn.net` / `api.curseforge.com` over
  https, x-api-key header from the payload (never logged), zip-safety per research 08 §4, and it
  runs ONLY after a matching `c` record with `ok=1` from the same character/session within 10 min.
  `addon.uninstall`: `{name?, projectId?, confirm:"player"}` — local only (never CurseForge),
  runs ONLY after the same matching `c` record. Resolution is exact and single-match: ledger
  `projectId`, then case-insensitive folder name, then unambiguous TOC `## Title` / recorded
  project name. No match → `not_installed`; more than one → `ambiguous` with the candidate folder
  names; either way nothing changes. `WoWHermes`, `WoWHermes_S###`, `Blizzard_*`, a link/reparse
  point and any path outside the direct AddOns folder are refused. On success the folder is
  **moved** (atomic rename, verified) to `<AddOns>\.wowhermes_backup\removed\<folder>-<stamp>`,
  never deleted, the install ledger entry gains `removedAt`/`removedBackup` (other entries are
  untouched), and the `reload` prompt above carries `disable=[<folder>]`. The notice reports the
  folder and backup path.
  `app.curseforge.status`: report whether the CurseForge app is installed (known dirs / protocol).
  `app.curseforge.install`: download the installer ONLY by resolving
  `https://www.curseforge.com/download/app` (hosts `www.curseforge.com`, `curseforge.com`,
  `*.forgecdn.net`), then run it with `confirm:"player"`.
  Anything else → `error:"unknown_action"`. The bridge reports each result as a `notice`. `done` marks the end of the agent turn for
  `reply_to`; it is emitted from `BasePlatformAdapter.on_processing_complete` (core
  `gateway/platforms/base.py:3418`, also on cancel/failure), so the bridge needs no idle
  heuristic. `next` = seq of the last returned frame (or `since` when empty). `reply_to` may be null for unsolicited
  messages (cron/send_message) — the bridge files those as a new exchange with a synthetic id.
  Retention: 512 frames or 24 h per character. `since` is the cursor/ack.
- Characters not in `WOW_ALLOWED_USERS` (comma list of `Name-Realm`) are rejected unless
  `WOW_ALLOW_ALL_USERS=true`.

## D. Bridge ↔ overlay
In-process (same Python app, Qt signals). The overlay shows the transcript per character
(persisted in `%LOCALAPPDATA%\wowhermes\transcripts.json`), typing state, bridge/capture health,
and lets the player move it away from the top-left strip area. It never sends input to the game.

## E. Non-negotiables
No input injection (keystrokes, mouse, `SendInput`, clipboard pasting into the game), no reading
game memory, no process handles to `Wow.exe`, no binding-carrier tricks. Capture is of the
screen/window image only.
