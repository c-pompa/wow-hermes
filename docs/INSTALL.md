# Install WoWHermes

WoWHermes is two pieces plus an addon:

| Piece | Runs on | What it does |
|---|---|---|
| **Hermes plugin** `wow` (`wowhermes-plugin-v0.2.0.zip`) | the machine that runs Hermes | gives Hermes a World of Warcraft chat platform, `/mode` toolsets and the WoW tools |
| **Bridge** (`wowhermes-bridge-v0.2.0.zip`) | the Windows PC that plays the game | reads the addon's status strip from the screen, talks to Hermes over HTTP |
| **Addon** `WoWHermes` (`WoWHermes-v0.2.0.zip`) | inside WoW: Forever | draws the chat window and the strip, receives replies |

The addon is also published on CurseForge, but CurseForge can only ship the addon. **The
bridge is a separate download and is required.** Without it the in-game window says
"bridge is down".

- Windows 11, PowerShell 5.1 or newer. No Git, no preinstalled Python, no SSH, no
  administrator rights.
- World of Warcraft: **Forever** (interface 16001), running in Windowed or
  Windowed (Fullscreen). Exclusive fullscreen cannot be captured.
- A Hermes Agent install (desktop app or CLI) somewhere on your network.

---

## 1. Hermes host — install the plugin

### Desktop app (recommended)

1. Unzip `wowhermes-plugin-v0.2.0.zip` into your Hermes plugins folder:
   - Windows: `%LOCALAPPDATA%\hermes\plugins\wow\`
   - macOS/Linux: `~/.hermes/plugins/wow/`
   The folder must contain `plugin.yaml` directly (`plugins\wow\plugin.yaml`).
2. In Hermes, open **Capabilities → Plugins**, refresh, and enable **World of Warcraft**.
   If it is not listed, restart the Hermes app once.
3. Enable the **Desktop** half of the plugin (Capabilities → Plugins → World of Warcraft →
   Desktop on), then open the **World of Warcraft** page.

### CLI

```bash
hermes plugins validate /path/to/plugins/wow     # must print "Validation passed."
hermes plugins enable wow
hermes plugins list | grep -i wow
```

### Configure the page

On the plugin page:

- **Character** — `Name-Realm` exactly as it appears in game, e.g. `Thrall-Area52`
  (realm spaces become hyphens: `Area 52` → `Area-52`).
- **Profile name** — a new dedicated Hermes profile for the game (default `wow`).
- **Gateway host / port** — the address the *game PC* will use to reach this machine.
  - Hermes and WoW on the same PC: `127.0.0.1`, port `8791`.
  - Hermes on another PC: this machine's LAN address (e.g. `192.0.2.10`), port `8791`.
- **Token** — click Generate. Copy the token once; it is shown only at that moment.
  You will paste it into the installer on the game PC's hidden prompt. Never put it in a
  command line, chat message or screenshot.

Save. The page creates/updates the profile, writes `WOW_BRIDGE_TOKEN`, `WOW_HOST`,
`WOW_PORT`, `WOW_ALLOWED_USERS`, enables the platform and waits for the listener. Status
should read *listening* and *connected* once the bridge is running.

CLI equivalent for the env values (in the profile's `.env`):

```
WOW_BRIDGE_TOKEN=<64 hex characters>
WOW_HOST=192.0.2.10      # or 127.0.0.1 for same-PC
WOW_PORT=8791
WOW_ALLOWED_USERS=Thrall-Area52
WOW_HOME_CHANNEL=Thrall-Area52
```

Restart the gateway for that profile after editing `.env` by hand.

> **If Hermes runs on a different PC**, `WOW_HOST` must be the LAN address, not the
> default `127.0.0.1` — otherwise the game PC cannot connect.

---

## 2. Game PC — run the bridge installer

1. Download `wowhermes-bridge-v0.2.0.zip` and its `SHA256SUMS` from the release page.
2. Verify the download (optional but recommended):

   ```powershell
   Get-FileHash .\wowhermes-bridge-v0.2.0.zip -Algorithm SHA256
   Get-Content .\SHA256SUMS | Select-String wowhermes-bridge
   ```

   The two hashes must match.
3. **Extract the zip** (right-click → Extract All). Do not run anything from inside the
   zip preview.
4. Open PowerShell in the extracted folder (`Shift` + right-click → *Open PowerShell
   window here*) and run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\bootstrap\install.ps1 `
       -WowDir "D:\Games\World of Warcraft\_forever_" `
       -GatewayUrl "http://192.0.2.10:8791"
   ```

   Replace the WoW path with yours and the gateway URL with the address shown on the
   Hermes plugin page. Omit `-WowDir` if the game is in a standard location — the
   installer probes Battle.net's usual folders, the registry and every fixed drive, and
   asks you to pass `-WowDir` only if it finds zero or several installs.
5. When prompted `Bridge token:`, paste the token. Input is hidden and is never echoed,
   logged or written to any command line.
6. Wait for `Done.` — the installer prints the addon counts, the status block and the
   log location.

Same-PC setup (Hermes desktop and WoW on one machine):

```powershell
powershell -ExecutionPolicy Bypass -File .\bootstrap\install.ps1 -LocalGateway
```

Installer flags (all optional except the gateway URL on a first install):

| Flag | Meaning |
|---|---|
| `-WowDir <path>` | `_forever_` folder; omit to auto-detect. |
| `-GatewayUrl <url>` | Hermes gateway base URL, e.g. `http://192.0.2.10:8791`. Required for a first install unless `-LocalGateway` is used or `config.toml` already exists. |
| `-LocalGateway` | Shortcut for `http://127.0.0.1:8791` when Hermes runs on this PC. |
| `-Token <SecureString>` | For automation only: pass the token as a `SecureString` (e.g. `-Token (ConvertTo-SecureString $env:WOW_TEST_TOKEN -AsPlainText -Force)`). Interactive users should use the hidden prompt instead — never paste a plain token into a command line, a script, or a log. |
| `-ForceToken` | Replace the token already stored in `config.toml` (prompts for the new one). |
| `-NoAutostart` | Do not register the logon task. |
| `-StartNow` | Start the bridge in the background after installing. |
| `-InstallDir <path>` | Per-user install root; default `%LOCALAPPDATA%\wowhermes`. |
| `-Slots <n>` | Number of load-on-demand slot addons; default and maximum tested value is 256. |
| `-DryRun` / `-WhatIf` | Print the plan, change nothing. |
| `-ProvisionTools` | With `-DryRun`: also verify the uv download, managed Python and `uv sync --frozen --no-dev`. |

### What the installer does

| Step | Detail |
|---|---|
| WoW discovery | `_forever_` from `-WowDir`, `WOWHERMES_WOW_DIR`, usual install paths, the registry, all fixed drives. Account is auto-detected from `WTF\Account`. |
| uv | uses `uv` from `PATH`, otherwise downloads the pinned official Astral release to `%LOCALAPPDATA%\wowhermes\tools\uv.exe` over TLS and verifies its SHA256 before running it. |
| Python | `uv python install 3.12` (managed interpreter under the install root — nothing is added to `PATH`). |
| venv | copies the bundled bridge source to `%LOCALAPPDATA%\wowhermes\bridge` and runs `uv sync --frozen --no-dev` into `%LOCALAPPDATA%\wowhermes\venv`. |
| config | writes `%LOCALAPPDATA%\wowhermes\config.toml` as UTF-8 **without BOM**. An existing token is preserved unless you pass `-ForceToken`. |
| addon | `wowhermes-bridge install --wow <dir> --addon-src <bundled addon> --slots 256`, then `status`. |
| autostart | registers the per-user scheduled task `WoWHermesBridge` (AtLogOn, interactive, unlimited duration, restart 3× / 1 min, ignore-new-instance). `-NoAutostart` skips it. |

Everything is idempotent: re-running the installer keeps files that already match and
updates the rest. `-DryRun` (alias `-WhatIf`) prints the plan and changes nothing;
`-DryRun -ProvisionTools` additionally verifies the uv/Python/venv bootstrap without
touching the config, the game or the task.

### After install

- Start/stop: `%LOCALAPPDATA%\wowhermes\bin\run.ps1` (foreground) and `stop.ps1`.
  With the scheduled task installed, start it from Task Scheduler or `Start-ScheduledTask
  -TaskName WoWHermesBridge`. `stop.ps1` and `uninstall.ps1` never touch the game process.
- Logs: `%LOCALAPPDATA%\wowhermes\logs\bridge.log` and `bridge.err.log`.
- Uninstall: `.\bootstrap\uninstall.ps1` (add `-RemoveAddon` to delete the addon from the
  game, `-Purge` to delete the saved token). Close WoW first.

---

## 3. In-game settings (required)

1. **Options → Graphics → Display Mode → Windowed (Fullscreen)** (or Windowed). The
   bridge captures the game window; exclusive fullscreen gives it a black frame.
2. **Keep the top-left corner clear.** The addon draws its status strip in the top-left
   **1600×400** pixels of the client area. Minimap addons, action bars, damage meters or
   chat overlays sitting there make frames unreadable. Move them, or hide them while
   playing. The strip is nearly transparent and only appears while a frame is pending.
3. Type `/reload` (or restart the game) so `WoWHermes` and its load-on-demand slot addons
   load. The addon list must show **WoWHermes** enabled; the `WoWHermes_S###` entries stay
   disabled on purpose — they load on demand.
4. Open the window with `/wowhermes` (or the minimap button / AddOn compartment icon) and
   send a test line. The reply proves bridge → Hermes → bridge → addon.

---

## 4. Network and firewall model

```
  WoW PC                                    Hermes host
  ┌─────────────────────────┐               ┌──────────────────────────┐
  │ addon  ── pixels ──►    │               │  wow adapter             │
  │        bridge           │ ── HTTP ────► │  listens on WOW_HOST:8791 │
  │  (outbound only)        │ ◄── long-poll │  127.0.0.1 or LAN IP      │
  └─────────────────────────┘               └──────────────────────────┘
```

- The bridge makes **outbound** HTTP requests only. It never listens. **Do not open port
  8791 on Windows** — if a firewall prompt appears, allow outbound/private and deny
  inbound.
- The listener lives on the **Hermes host**. Bind it to `127.0.0.1` when Hermes and the
  game share a PC. Bind it to the LAN IP when they don't, and create an inbound rule on
  the **Hermes host** limited to the game PC's address (or to your LAN subnet), port
  8791/TCP.
- HTTP is plain on the LAN; the bearer token is the only authentication. Treat the LAN
  (or a VPN) as the trust boundary — do not expose 8791 to the internet, do not use
  `0.0.0.0` on a shared network, and rotate the token from the plugin page if it leaks.
- The character name is asserted by the bridge, not Blizzard; the token, not the name,
  is what authenticates a client.

---

## 5. CurseForge companion model

`WoWHermes-v0.2.0.zip` is the CurseForge/Wago-ready addon: exactly the files listed in
`WoWHermes.toc` plus `media/`. Installing it from CurseForge gives you the in-game window
and nothing else — CurseForge cannot distribute the Python bridge. The addon will show
"bridge is down" until the bridge zip from this release is installed on the same PC and
running. Keep the addon and bridge on the same release version (both `v0.2.0`); the
release manifest records the addon's own source version.

---

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `no World of Warcraft: Forever install found` | Pass `-WowDir "<...>\_forever_"`. The path must end in `_forever_` and contain `WTF\` and `Interface\AddOns\`. |
| `more than one WoW install was found` | Pass `-WowDir` with the live one (the list is printed). |
| `-GatewayUrl is required for a first install` | Add `-GatewayUrl http://<hermes-host>:8791`, or `-LocalGateway` if Hermes runs on this PC. |
| `could not download uv ...` | Corporate proxy/TLS. Install uv from https://docs.astral.sh/uv/ so it is on `PATH`, then re-run. |
| `uv sync failed` | Usually the same proxy/TLS issue or no disk space. Run `uv sync --frozen --no-dev --directory %LOCALAPPDATA%\wowhermes\bridge` manually to see the full error. |
| `running scripts is disabled on this system` | The `-ExecutionPolicy Bypass` in the command above fixes it per run. Do not change the machine policy for this. |
| In-game: "Hermes bridge is down" | The bridge is not running (check `logs\bridge.log`), the scheduled task is disabled, or the token/URL is wrong. `wowhermes-bridge status` from `%LOCALAPPDATA%\wowhermes\venv\Scripts` shows the effective config (never the token). |
| `401` in `bridge.err.log` | Token mismatch. Re-run the installer with `-ForceToken` and paste the current token from the plugin page. |
| `no WoW window found` | The game is in exclusive fullscreen, minimised, or on another virtual desktop. Switch to Windowed (Fullscreen). |
| Strip never clears / replies never appear | Something covers the top-left 1600×400 area, or the UI scale is extreme. Move overlays, keep default UI scale, `/reload`. |
| Task exists but does nothing at logon | The task runs only for the user who installed it, in an interactive session. Re-run `install.ps1` (without `-NoAutostart`) as that same user. |
| Antivirus quarantines `uv.exe` | It is the official Astral binary and its SHA256 is pinned in `install.ps1`; allow it, or install uv yourself and re-run. |
| Hermes plugin page shows "adapter offline" | `WOW_HOST` is still `127.0.0.1` on a multi-PC setup, or the gateway was not restarted after the `.env` change. |
