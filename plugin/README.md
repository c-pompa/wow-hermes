# `wow` — Hermes platform plugin for World of Warcraft: Forever

The gateway-side half of WoWHermes. The companion bridge next to the game client POSTs chat
lines to this adapter and long-polls an outbox for replies. One character (`Name-Realm`) = one
Hermes DM session.

Wire contract: [`docs/PROTOCOL.md`](../docs/PROTOCOL.md) §C. Implementation notes, hook
findings and troubleshooting: [`docs/notes/ws-plugin.md`](../docs/notes/ws-plugin.md).

```
hermes_plugin/wow/
  plugin.yaml     manifest (kind: platform, name wow-platform, env declarations)
  __init__.py     import-light: defers adapter.py until the platform is asked for
  adapter.py      WoWAdapter(BasePlatformAdapter) + register(ctx)
  tests/
    plugin_loader.py     loads adapter.py by path (hermes-agent loader pattern)
    test_wow_plugin.py   62 tests: registration, auth, validation, inbound, outbox, done
    live_check.py        standalone adapter + curl transcript, no LLM, no gateway
```

## Endpoints (default `127.0.0.1:8791`)

| Route | Auth | Purpose |
|---|---|---|
| `GET /v1/wow/health` | none on loopback, bearer otherwise | liveness (`{ok,platform,version,connected,characters}`) |
| `POST /v1/wow/message` | `Authorization: Bearer $WOW_BRIDGE_TOKEN` | one inbound chat line → `MessageEvent`; `202 {ok,accepted[,reason]}` |
| `GET /v1/wow/outbox?character=&since=&wait=` | bearer | long-poll `reply`/`typing`/`done` frames, `wait` 0–25 s |

`202 accepted:false` reasons: `duplicate`, `unauthorized` (character not in the allowlist).
A missing/wrong token is `401`. Bodies > 64 KiB are `413`. Bad JSON/character/text/msg_id are
`400` with `{ok:false,error:<code>}`.

## Deploy — a NEW dedicated profile named `wow`

> **These steps are for the coordinator. They were NOT executed by WS-C.** The machine runs a
> multiplexed gateway serving 15 profiles (DEC-009): never restart it as part of a plugin test.
> Read the whole section before running anything.

### 1. Create the profile

```bash
hermes profile create wow            # -> /Users/you/.hermes/profiles/wow
hermes profile show wow              # confirm path, model, skills
export WOW_HOME_DIR="$HOME/.hermes/profiles/wow"
```

A dedicated profile is deliberate: the WoW surface gets its own sessions, model choice,
allowlist and `WOW_BRIDGE_TOKEN`, and cannot widen the blast radius of the main profile.

### 2. Install the plugin (copy, not symlink, so profile archives are self-contained)

```bash
mkdir -p "$WOW_HOME_DIR/plugins"
rsync -a --delete \
  --exclude '__pycache__' --exclude '.pytest_cache' \
  /src/wow-hermes-plugin/hermes_plugin/wow/ \
  "$WOW_HOME_DIR/plugins/wow/"

# static gates (both are read-only)
hermes plugins validate "$WOW_HOME_DIR/plugins/wow"
hermes -p wow plugins list | grep -i wow
```

`hermes plugins validate` must print `Validation passed.` (it runs `register()` in isolation,
scans for core overrides and checks the capability manifest).

### 3. Credentials and settings — that profile's `.env`

Env vars live in the profile's `.env` (there is no `config set-env` verb):

```bash
hermes -p wow config env-path            # -> /Users/you/.hermes/profiles/wow/.env
TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')

cat >> "$(hermes -p wow config env-path)" <<EOF
WOW_BRIDGE_TOKEN=$TOKEN
WOW_HOST=192.0.2.10
WOW_PORT=8791
WOW_ALLOWED_USERS=Thrall-Area52,Jaina-Proudmoore
WOW_HOME_CHANNEL=Thrall-Area52
EOF
```

Interactive alternative: `hermes -p wow gateway setup` offers the platform and prompts for the
same keys (the manifest's `requires_env`/`optional_env` descriptions and the `WOW_BRIDGE_TOKEN`
password flag come from `plugin.yaml`).

- `WOW_HOST` — default is `127.0.0.1`, which the game PC cannot reach. For the real deployment
  set the VLAN-15 address `192.0.2.10`, or `0.0.0.0` if the address can change. Opening it to
  the LAN makes `WOW_BRIDGE_TOKEN` the only trust boundary: use a 64-hex-char token, never log
  it, and firewall `8791` to VLAN 15 only.
- `WOW_ALLOWED_USERS` — comma-separated `Name-Realm` keys, case-insensitive; a realm with spaces
  is written with hyphens (`Area 52` → `Area-52`). Leave it empty to let the gateway's normal
  authorization ladder (pairing) decide; set it for a locked-down install.
- `WOW_BRIDGE_TOKEN` — if unset the adapter generates one in memory at startup and logs it as
  being unset; the bridge then cannot know it, so set it explicitly.

#### CurseForge key — one per machine, not per profile

The CurseForge Core API key belongs to the player, not to a character or profile, so it is stored
once per machine and shared by every Hermes profile on it:

```
<hermes root>/plugin-data/wow/curseforge.key     # one line, 0600, atomic write
```

`<hermes root>` is the Hermes home with any `profiles/<name>` tail removed — `~/.hermes` on this
Mac (so `default`, `wow`, `gamedev-s2` all resolve to the same file) and `%LOCALAPPDATA%\hermes`
on Windows (shared by the profiles under it). It is **not** synced between machines.

- Preferred: save it once through the desktop setup form / `POST /api/plugins/wow/setup`
  (`curseforge_api_key`). That writes the shared file and removes any profile `.env` copy so the
  key is stored once.
- `curseforge_api_key: "clear"` deletes the shared file **and** the target profile's
  `WOW_CURSEFORGE_API_KEY`; empty/absent leaves everything unchanged.
- Resolution order at tool time: shared file first, then this profile's scoped
  `WOW_CURSEFORGE_API_KEY` (backward compatibility for older installs), then no key. A shared file
  that exists always wins, so rotating it through any profile changes it for all of them.
- An unreadable shared file falls back to the env with a non-secret warning (path + error type
  only). The key and the file contents are never returned, logged, or shown in `/status`; `/status`
  reports only `curseforge_key_set` and `curseforge_key_scope` (`shared` | `profile` | `null`).

Full semantics: `docs/notes/ws-shared-key.md`.

### 4. `config.yaml` (optional — env alone is enough)

Env seeding already populates `extra`, so the only mandatory key is enablement. If you prefer
YAML (it also documents the shape for readers):

```yaml
# $WOW_HOME_DIR/config.yaml
gateway:
  platforms:
    wow:
      enabled: true
      extra:
        host: "192.0.2.10"
        port: 8791
        # allowed_characters: "Thrall-Area52"   # advisory only; the env var is authoritative
```

Do not hand-edit if you can use `hermes -p wow config set gateway.platforms.wow.extra.host …`;
env beats YAML for every key (`_shared.extra_or_secret` reads the env first).

### 5. Enable the plugin

```bash
hermes -p wow plugins enable wow        # writes plugins.enabled in that profile's config
hermes -p wow plugins doctor wow        # runtime-contract check (optional)
```

### 6. Start the gateway — the only step with blast radius

Two shapes; pick one with the user, not unilaterally:

- **Dedicated per-profile gateway** (isolated, recommended for a first rollout):
  `hermes -p wow gateway install` then `hermes -p wow gateway start`. It binds `WOW_PORT` from
  its own config and takes the scoped lock `wow-bridge:192.0.2.10:8791`.
- **Serve `wow` from the existing multiplexed gateway** (no new port, shares the process):
  enable the plugin in that profile, then restart the multiplexer **at a planned, user-approved
  moment**. Never restart it to test the plugin.

Starting the dedicated profile gateway does not touch the multiplexed one; only one of the two
may own `host:port` — a second `connect()` fails with `wow-bridge endpoint … already in use`
(`_acquire_platform_lock`, `gateway/platforms/base.py:2143`). Give each profile its own
`WOW_PORT` if both must run.

### 7. Verify

```bash
hermes -p wow gateway status | grep -i "world of warcraft"
hermes -p wow gateway logs | grep '\[wow\]'      # "Bridge endpoint listening on http://…"

# from the Mac itself (loopback health needs no token)
curl -s http://127.0.0.1:8791/v1/wow/health
# from game-pc (token required; use the value from step 3)
curl -s -H "Authorization: Bearer $TOKEN" http://192.0.2.10:8791/v1/wow/health
```

Expected: `{"ok": true, "platform": "wow", "version": 1, "connected": true, "characters": []}`.

### 8. Cron / `send_message`

`cronjob(deliver="wow")` routes to `WOW_HOME_CHANNEL`, but delivery needs a **live** adapter in
the same process: the outbox is in-memory (the bridge long-polls it over HTTP). `standalone_sender_fn`
therefore returns an explicit error rather than pretending to deliver. A cron job created from
inside a WoW session works normally — it inherits the live gateway.

## Security model

1. **Bearer token** on every route except loopback health (`hmac.compare_digest`). This is the
   only real trust boundary: the character name is asserted by the bridge, not by Blizzard.
2. **Allowlist** (`WOW_ALLOWED_USERS` / `WOW_ALLOW_ALL_USERS`) is checked here and by the
   gateway core; it is attribution and convenience, not authentication.
3. **Body cap 64 KiB**, strict `Name-Realm` regex, dedupe by `msg_id`, no token ever logged.
4. `trusted_inbound` stays **False**; `allow_update_command` is **False** (a `/update` issued
   from inside the game must not restart the gateway mid-instance).
5. **CurseForge key** lives in a `0600` machine-wide file (`plugin-data/wow/curseforge.key`) so one
   key serves every profile on this host; per-profile env remains only as a fallback. Unreadable
   file → env fallback with a path-only warning. Never returned, logged, or echoed by `/setup` or
   `/status`.

## Development

```bash
cd /src/wow-hermes-plugin
PYTHONPATH=$HOME/.hermes/hermes-agent \
  "$HOME/.hermes/hermes-agent/.venv/bin/python" -m pytest hermes_plugin/wow/tests -q
PYTHONPATH=$HOME/.hermes/hermes-agent \
  "$HOME/.hermes/hermes-agent/.venv/bin/python" hermes_plugin/wow/tests/live_check.py
```

The tests load `adapter.py` by path (nothing is copied into the Hermes checkout) and the live
check binds an ephemeral `127.0.0.1` port with a throwaway `HERMES_HOME`.
