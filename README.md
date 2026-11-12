# WoW Hermes

Talk to a Hermes agent from inside **World of Warcraft: Forever**.

WoW Hermes connects the game chat to a Hermes profile: a companion bridge next to
the game captures your chat lines and writes the agent's replies back into the
client through the addon. One character (`Name-Realm`) is one Hermes DM session.

This repository is the **public release mirror** of the project. It is a
sanitized snapshot of a private development tree, not that tree's history. The
Hermes catalog entry for this plugin has been prepared but **is not listed yet**;
until a maintainer merges it, install from this repository at a pinned commit as
shown below.

## What you get

- **In-game chat platform for Hermes** — `kind: platform` gateway adapter with a
  local HTTP endpoint (`/v1/wow/message`, `/v1/wow/outbox`, `/v1/wow/health`),
  bearer-token authentication, one DM session per character.
- **Three per-character toolsets** selected with `/mode`:
  - `standard` — everyday Hermes tools, including quest and addon help.
  - `wow` — WoW-only tools plus web search; the small surface for play.
  - `full` — the whole Hermes toolset. Requires an in-game confirmation and
    lapses back to `standard` after 60 minutes.
- **Five WoW tools** — `wow_addon_search`, `wow_addon_guide`,
  `wow_addon_install`, `wow_addon_uninstall`, `wow_quest_help`.
- **Bundled skill** — `wow-player-guide` explains the modes, the in-game channel
  rules, and the addon workflows.
- **Desktop setup page** — creates or updates a dedicated Hermes profile
  (`wow`), writes its environment and gateway config, renders `SOUL.md` and a
  profile skill, and shows adapter status and the one-time bridge token.
- **Addon management** — search CurseForge for Forever-compatible addons,
  queue an install for the bridge, and uninstall by moving the folder to a
  removal backup. The player confirms every write in game.

## Requirements

| Part | Requirement |
|---|---|
| Hermes | **0.22.7 or newer** (validated on 0.22.7). The catalog entry declares the same floor. |
| Game | **World of Warcraft: Forever**, Interface **16001** (build 1.60.1.70862). |
| Bridge host | **Windows 11**, on the same PC as the game or on the same trusted network. `uv` downloads a private Python for the bridge. |
| Network | The Windows bridge must reach the Hermes listener over HTTP. Loopback when both run on the same PC; otherwise a LAN/VPN address you choose and firewall. |
| CurseForge (optional) | Your own CurseForge Core API key, only for one-click addon installs. Without it the addon tools return a CurseForge deep link. |

macOS and Linux can host Hermes and the plugin; the bridge itself is
Windows-only.

## Install the Hermes plugin

Choose a commit in this repository and install its `plugin/` subdirectory:

```bash
hermes plugins install c-pompa/wow-hermes/plugin --ref <40-hex-commit-sha> --enable
```

`--ref` takes the full 40-character SHA; branches and short SHAs are refused.
`--enable` selects the plugin in the active profile. After a catalog listing is
merged, `hermes plugins install wow` will install the reviewed pin instead.

To validate a checkout before installing:

```bash
hermes plugins validate plugin/
```

### Set up the profile

**Desktop (recommended).** Open Hermes Desktop → Plugins → **WoW Hermes**.
Fill in your character (`Name-Realm`, e.g. `Thrall-Area52`), the profile to
create (`wow` by default), the starting mode, the listener host and port, and
optionally your CurseForge key. Save. The page creates the profile, writes its
`.env` and `config.yaml`, renders `SOUL.md` and a profile skill, and shows the
gateway URL and a one-time bridge token. If the profile's gateway is already
running under a multiplexer it may be hot-served; otherwise start it:

```bash
hermes -p wow gateway start
```

**CLI.** The same result without Desktop:

```bash
hermes profile create wow
hermes -p wow plugins enable wow

# Values live in this profile's .env (ask Hermes for the path):
hermes -p wow config env-path
```

Add to that file:

```dotenv
WOW_BRIDGE_TOKEN=<64 hex characters>
WOW_HOST=127.0.0.1
WOW_PORT=8791
WOW_ALLOWED_USERS=Thrall-Area52
WOW_HOME_CHANNEL=Thrall-Area52
```

`WOW_HOST` defaults to `127.0.0.1`. Set it to the address the Windows bridge
will dial only if the bridge runs on another machine, and firewall the port to
that machine. Then enable the platform and start the gateway:

```bash
hermes -p wow config set gateway.platforms.wow.enabled true
hermes -p wow gateway start
curl -s http://127.0.0.1:8791/v1/wow/health
```

Expected: `{"ok": true, "platform": "wow", "version": 1, ...}`. A second
gateway cannot bind the same host:port; give each profile its own `WOW_PORT`.

`WOW_BRIDGE_TOKEN` is the shared secret the bridge sends as
`Authorization: Bearer …`. If it is unset the adapter generates one in memory
at startup and the bridge cannot know it, so set it explicitly. The character
name is asserted by the bridge, not by Blizzard; the token is the only real
authentication boundary.

## Install the addon and the bridge

The bridge is a separate program. It captures a 1600×400 pixel strip from the
game window, decodes chat, forwards it to Hermes, and writes replies back into
the addon's slot files in the WoW `AddOns` tree. It also installs the addon and
its 256 load-on-demand slot addons.

Download `wowhermes-bridge-v0.2.0.zip` from the release and extract it. The
bundle includes the addon, locked bridge source and a PowerShell bootstrap. From
the extracted directory, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\bootstrap\install.ps1 `
    -WowDir "D:\Games\World of Warcraft\_forever_" `
    -GatewayUrl "http://192.0.2.10:8791"
```

The installer prompts for the one-time bridge token without echoing it. It finds
or downloads a checksum-verified `uv`, installs a private Python 3.12, creates a
locked virtual environment, installs the addon and slot addons, writes UTF-8
(no-BOM) configuration, and can register a per-user AtLogOn task. No Git,
preinstalled Python, SSH or administrator rights are required. Use `-LocalGateway`
instead of `-GatewayUrl` when Hermes runs on the same PC, `-DryRun` to preview,
and `-NoAutostart` for manual-only startup.

For a source-only alternative, `uv run wowhermes-bridge install --wow <path>`
and `uv run wowhermes-bridge run` work from `bridge/` after `uv sync --frozen`.
The bridge must be running whenever you use chat in game. Verify release files
against `SHA256SUMS` before installing. Nothing in the plugin downloads or
replaces the bridge for you.

CurseForge addon availability is **separate** from this repository and from the
Hermes plugin: the addon tools query CurseForge, and an addon installs only when
its author allows third-party distribution and a Forever-compatible file
exists. Otherwise the tool returns a `curseforge://` link for the player to
finish in the CurseForge app.

## Security and privacy

Read this before installing. The full disclosure is also in the Hermes catalog
entry and the upstream pull request.

- **Local HTTP listener.** The plugin opens `WOW_HOST:WOW_PORT` (loopback by
  default; optionally a LAN address) with plain HTTP. `/v1/wow/message` and
  `/v1/wow/outbox` require `Authorization: Bearer $WOW_BRIDGE_TOKEN`;
  `/v1/wow/health` needs no token on loopback. There is no TLS: use loopback, a
  trusted LAN/VPN, and a firewall rule that admits only the bridge host.
- **Bearer token.** A wrong or missing token is `401`. The token is never
  logged or returned by the status page. The character name is bridge-asserted
  (`WOW_ALLOWED_USERS` is attribution/convenience, not authentication).
- **Screen capture.** The Windows bridge captures pixels from the game window
  with the Windows Graphics Capture API (mss/GDI fallback). It is restricted to
  the addon's chat strip; it does not read the game process or its memory,
  inject input, or automate gameplay.
- **Files outside the plugin's data directory.** The bridge writes the addon
  and slot files under the WoW `AddOns` directory and reads SavedVariables for
  reload mode. Uninstall moves the addon folder into a removal backup; it does
  not permanently delete it.
- **Outbound network.** The bridge makes HTTP requests to the Hermes host you
  configure. The plugin makes CurseForge API calls (`api.curseforge.com`) with
  your key. The key is stored once per machine at
  `plugin-data/wow/curseforge.key` (mode `0600`), with the profile's
  `WOW_CURSEFORGE_API_KEY` as a fallback; it is never returned to the model or
  logged.
- **Model provider.** Chat text and tool results from the game go to whichever
  model provider the `wow` Hermes profile is configured to use — cloud or
  local. That provider's data-handling terms apply, and token spend is yours.
  Tool results can include addon metadata and quest text.
- **Local tools and modes.** `standard` and `wow` keep the surface small;
  `full` exposes the whole Hermes toolset (terminal, files, desktop control)
  until it expires. `full` requires an in-game confirmation.
- **Background process.** The bridge is a long-running process next to the
  game; it must stay running for chat to work.
- **One-time setup writes.** The desktop setup page creates/updates the `wow`
  profile, its `.env`, `config.yaml`, `SOUL.md`, and a profile skill, and may
  ask a running gateway multiplexer to serve the new profile. It does not
  restart the game or the Hermes gateway.
- **No telemetry.** The plugin and bridge send nothing to the project author.
- **Compatibility.** Requires Hermes 0.22.7+, Windows 11 for the bridge, and
  WoW: Forever (Interface 16001). Other clients/versions are not supported.

## Known limitations

- The game must run **windowed or borderless-windowed**; exclusive fullscreen
  exposes no capturable window.
- One character = one Hermes session; the bridge reports the character name.
- Addon installation depends on CurseForge metadata and the author's
  distribution setting, and is never silent: the player confirms in game.
- The public repository is a release mirror; older development commits and
  internal notes are not published.
- Cron/notification delivery to `wow` needs a live adapter in the same gateway
  process; the outbox is in memory.

## License

MIT. See [LICENSE](LICENSE). Third-party design credits and prior art are in
the bridge package's `NOTICE` file.
