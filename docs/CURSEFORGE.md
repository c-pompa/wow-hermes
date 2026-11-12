# WoW Hermes

**CurseForge project title:** `WoW Hermes`

**Summary (one paragraph, paste into the Summary field):**

> Talk to your own Hermes AI agent from inside World of Warcraft: Forever. WoW Hermes adds a
> native in-game window, a minimap button and a dedicated chat tab; each character gets its own
> persistent Hermes conversation with switchable tool modes, quest help and addon install help.
> The addon is the in-game half — it needs the free Hermes Agent, its `wow` platform plugin and
> the Windows companion bridge to reach the model.

---

## Description

**WoW Hermes brings your Hermes Agent into Azeroth — as a normal addon window, not an overlay
on top of the game.**

Ask a question in the Hermes window (or type `/h <text>` in chat) and the answer appears in the
same window, in WoW's own fonts and colours, with the transcript kept per character. No
Alt-Tab, no second screen, no copy-pasting between the game and a browser.

### What you get in game

- **Hermes window** — a Blizzard-native frame with a transcript, an input box, Send, and
  Markdown-lite formatting (gold headings, bullets, code, links). It opens on `/h <text>`,
  opens again when a reply finishes, and echoes each finished reply once into chat so you can
  keep playing.
- **Minimap button** — left-click opens the Hermes window, right-click opens settings. The
  tooltip shows the live bridge state and how many replies are waiting.
- **Hermes chat tab** — a separate chat-frame tab with a one-line hint, for players who prefer
  the chat log to a floating window.
- **Comfort features** — mouse-wheel and Shift-page transcript scrolling, a resize grip with a
  saved size, a fade-while-moving option, and saved notes you can keep between sessions
  (`Save` / `Notes` / `Clear` / `New`).
- **Per-character sessions** — one character (`Name-Realm`) is one Hermes conversation. Your
  warrior's questions do not land in your druid's thread.
- **Modes** — `/mode standard` (default: normal assistant tools, no shell), `/mode wow`
  (addon search, addon guide, quest help, web) and `/mode full` (the whole toolset, time-boxed
  to 60 minutes and requiring an explicit `/mode full confirm`). A mode change applies to your
  next message, in the same session, with no restart.
- **Quest and addon help** — ask about a quest and the agent can look it up; ask for an addon
  and it can search CurseForge, hand you a one-click `curseforge://install` link, or offer a
  guided install. Installs that touch your AddOns folder always show an in-game confirmation
  window first and end with a manual `/reload` reminder — the addon never reloads your UI by
  itself.

### Requirements (read this first)

The CurseForge download is **only the in-game addon**. It cannot reach a model on its own.
You also need:

1. **Hermes Agent** — the assistant runtime, running on a machine you control.
2. **The Hermes `wow` platform plugin** — creates the per-character sessions, the modes and
   the WoW tools.
3. **The WoW Hermes bridge** — a small companion app that runs next to the game client on
   **Windows**, reads the addon's pixel strip and delivers replies. This is the only component
   that does screen capture, and it is Windows-only.

Install and setup guide (public docs): **https://github.com/c-pompa/wow-hermes**
Bridge and addon bundle: **https://github.com/c-pompa/wow-hermes/releases/tag/v0.2.1**

### How it works (and what it never does)

- Outbound messages are drawn by the addon as a **pixel strip** in the top-left of the client;
  the bridge photographs that strip and decodes it. Replies come back as ordinary addon files
  the bridge writes into your AddOns folder.
- The addon **never reads chat**, never touches the game process or its memory, never injects
  input, and never opens a network connection. Drawing pixels and reading addon files are the
  only channels — the same approach used by other Forever chat bridges.
- Chat lockdown and secret values on Forever are respected: the addon only sends what **you**
  type into its own input box.

### Install (addon)

1. Close World of Warcraft.
2. Install this addon with the CurseForge app into your **Forever** installation, or unzip it
   so that `WoWHermes/WoWHermes.toc` sits inside
   `World of Warcraft\_forever_\Interface\AddOns\`.
3. Start the bridge **before** launching the game, then log in and `/reload` once.
4. Click the minimap button (or type `/h hello`). If the tooltip says `Bridge: offline`, the
   bridge or plugin is not running — see the setup guide.

### Limitations

- **World of Warcraft: Forever only** (Interface `16001`, client 1.60.1). Not built or tested
  for Retail, Classic Era, or any other flavour, and it will not be offered for them.
- **Windows for the bridge.** The addon itself is platform-independent; the capture component
  is Windows-only.
- **Windowed or borderless mode.** The bridge photographs the client window; exclusive
  fullscreen has nothing to capture.
- **The addon never reads chat.** Guild, party, whisper and say channels are invisible to it.
- **No addon-initiated reload.** Forever blocks it, so installs end with a visible
  `Type /reload` reminder you press yourself.
- **Replies are short by design.** The transport favours a few sentences over essays; long
  answers are shortened in the window rather than dumped as a wall of text.
- **Not an automation tool.** It will not play, move, click, or act for you.

### Support and source

- Documentation / setup: https://github.com/c-pompa/wow-hermes
- Issues: https://github.com/c-pompa/wow-hermes/issues
- Please include your client build, interface number, bridge log tail, and whether the minimap
  tooltip says `Bridge: connected` — those four answers resolve most reports.

### License

MIT. The addon TOC, the Hermes plugin manifest and the bridge package all declare MIT.
