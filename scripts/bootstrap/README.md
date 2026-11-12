# WoWHermes bridge (Windows companion)

This package contains everything needed to run the WoWHermes bridge on the PC that
plays World of Warcraft: Forever. It does **not** need Git, a preinstalled Python, or SSH.

```
bootstrap/install.ps1      per-user installer (uv + managed Python 3.12 + venv)
bootstrap/run.ps1          start the bridge
bootstrap/stop.ps1         stop the bridge
bootstrap/uninstall.ps1    remove the install
bridge/                    bridge source (pyproject.toml, uv.lock, wowhermes_bridge/)
addon/WoWHermes/           the in-game addon the installer copies into the game
docs/INSTALL.md            step-by-step install, firewall and troubleshooting
LICENSE                    MIT
NOTICE                     third-party notices
```

Quick start (PowerShell, from the extracted folder):

```powershell
powershell -ExecutionPolicy Bypass -File .\bootstrap\install.ps1 `
    -WowDir "D:\Games\World of Warcraft\_forever_" `
    -GatewayUrl "http://192.0.2.10:8791"
```

The installer asks for the bridge token with hidden input. Never pass the token on a
command line. Read `docs/INSTALL.md` first: the game must run in Windowed (Fullscreen)
and the top-left strip area must stay unobscured. This PC never listens on a port
(the bridge only makes outbound HTTP requests to the Hermes gateway).

The addon alone is also published separately for CurseForge; that download is **not**
enough -- the bridge in this zip is required.
