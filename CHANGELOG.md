# Changelog

All notable changes to WoWHermes are recorded here. The project ships three components
that version independently but are released together: the Hermes plugin `wow`, the WoW
Forever addon `WoWHermes`, and the Windows bridge `wowhermes-bridge`. Release tags use
the plugin version (`v0.2.0`); `release-manifest.json` records each component's source
version inside that release.

## [v0.2.0] — 2026-11-11

First release packaged for people who are not the author.

### Added — install and release engineering

- `wowhermes-bridge-v0.2.0.zip`: source bootstrap installer for Windows 11 / PowerShell
  5.1+. No Git, no preinstalled Python, no SSH, no administrator rights. Resolves or
  downloads a SHA256-pinned `uv`, installs managed CPython 3.12, runs
  `uv sync --frozen --no-dev`, writes `%LOCALAPPDATA%\wowhermes\config.toml` (UTF-8, no
  BOM, token never printed), installs the addon and 256 slot addons, and registers an
  AtLogOn per-user scheduled task. Includes `run.ps1`, `stop.ps1`, `uninstall.ps1` and
  `-DryRun`/`-WhatIf`.
- Deterministic `scripts/build_release.py`: plugin/addon/bridge zips, `SHA256SUMS`,
  `release-manifest.json` with the source commit, component versions and per-file
  SHA256, plus the docs.
- `LICENSE` (MIT) and `NOTICE` (third-party licenses, Hermes icon-art provenance,
  Blizzard/Nous Research non-affiliation).
- `docs/INSTALL.md` (end-user install, in-game settings, firewall model, CurseForge
  companion model, troubleshooting) and `docs/RELEASE.md` (reproducible build and
  release checklist).

### Plugin `wow` 0.2.0

- One installable Hermes package (package name `wow`, kind `platform`) that carries the
  adapter, tools, skills and desktop setup page together.
- Platform adapter with bearer-token authentication, body cap, `Name-Realm` validation,
  dedupe and per-character DM sessions; `/v1/wow/message`, `/v1/wow/outbox` long-poll and
  `/v1/wow/health`.
- `/mode standard | wow | full` per character at runtime, with a time-boxed, two-step
  `full` mode and an announced expiry.
- WoW tools: `wow_addon_search`, `wow_addon_guide`, `wow_addon_install`,
  `wow_addon_uninstall`, `wow_quest_help`.
- Desktop setup page: character, profile, default mode, host/port; creates the profile,
  writes the env/config, enables the platform, shows adapter/bridge status, and stores
  one player CurseForge key per machine (`plugin-data/wow/curseforge.key`, never echoed).
- Bundled `wow-player-guide` skill and a `SOUL.md` template for the game profile.
- Reload/enable-on-install flow for installed addons; install/uninstall are ledger-backed
  and use backups outside the AddOns search path.

### Addon `WoWHermes` 0.2.0

- In-game Hermes window (Blizzard-native frame): transcript, input, Send, mouse-wheel and
  Shift-page scrolling, saved notes (local), copy, resize with saved size, fade-on-move.
- Status strip in the top-left of the client area (pixel transport) plus a `/reload`
  SavedVariables fallback, 256 load-on-demand slot addons for reply delivery.
- Addon-owned prompts for install confirmation and the manual `/reload` reminder — no
  Blizzard dialog API, keyboard capture, gamepad registration or action-bar attribute
  writes (Forever blocks those; a test scans every TOC file for them).
- Quest snapshot record, minimap button, AddOn compartment entry, options panel
  (fade, doorbells, gamepad navigation opt-in) and Markdown-lite formatting.
- Hermes icon art derived from the Hermes Agent desktop assets (see `NOTICE`).

### Bridge `wowhermes-bridge` 0.2.0

- Window capture (Windows Graphics Capture, GDI fallback) of the top-left 1600×400 region
  and an any-size pixel-strip decoder.
- Atomic publishing to all 256 slot files, silent-WAV doorbells, reply ladder and ack
  high-water tracking.
- Hermes gateway HTTP client with long-poll outbox, typing/done frames, hello/handshake
  and reconnect.
- `install` (addon + slot addons + ctl/pools, idempotent, `--dry-run`), `run`, `status`,
  `decode-image`, `probe-capture`; safe addon-zip extraction and protected-folder rules
  for install/uninstall.
- Optional PySide6 overlay extra.

### Notes and limitations

- No native `.exe` yet: the bridge runs from a uv-managed Python 3.12 environment. A
  self-contained executable is a later task.
- Windows-only capture stack (`mss`, `windows-capture`) is marker-gated; macOS/Linux
  checkouts can run the tests but not capture.
- The bridge stores its token in `%LOCALAPPDATA%\wowhermes\config.toml` (per-user ACL).
  `WOWHERMES_TOKEN` still overrides the file.
