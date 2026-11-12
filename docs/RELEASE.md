# Release process

Reproducible build + verification for a WoWHermes release. Nothing here publishes
anything; uploading the artifacts is a separate, explicit step.

## Prerequisites

- Python 3.9+ (stdlib only — the builder imports `scripts/package_addon.py` for the
  addon TOC validation and uses `zipfile`/`hashlib`).
- A git checkout of this repository. The manifest records `HEAD` and whether the tree
  was dirty.
- For the Windows bootstrap proof: a Windows 11 machine with PowerShell 5.1+, internet
  access, and a **scratch** directory. Never point a test run at a live game install.

## 1. Pick the version

- `hermes_plugin/wow/plugin.yaml` `version:` is the release label (`0.2.0` → `v0.2.0`).
- `addon/WoWHermes/WoWHermes.toc` `## Version:` and `bridge/pyproject.toml` `version`
  are component source versions; they may differ from the release label.
- `CHANGELOG.md` gets a section for the release.
- The bootstrap pins (`uv` version + SHA256, Python minor) live in
  `scripts/bootstrap/install.ps1`; the manifest copies them, so there is one source of
  truth.

Bumping the release is: edit the four places, commit, build.

## 2. Build

```bash
cd <checkout>
python3 scripts/build_release.py --version 0.2.0
# or, without writing anything:
python3 scripts/build_release.py --version 0.2.0 --check-only
```

Output in `dist/v0.2.0/`:

| File | Contents |
|---|---|
| `wowhermes-plugin-v0.2.0.zip` | native Hermes package, rooted at `wow/` (plugin.yaml, adapter, tools, skills, dashboard, desktop page; no tests) |
| `WoWHermes-v0.2.0.zip` | addon only: TOC-listed files + `media/`; CurseForge/Wago-ready |
| `wowhermes-bridge-v0.2.0.zip` | bridge source (`pyproject.toml`, `uv.lock`, `.python-version`, `wowhermes_bridge/`), bundled addon, `bootstrap/*.ps1`, `README.md`, `LICENSE`, `NOTICE`, `docs/INSTALL.md` |
| `SHA256SUMS` | `sha256  filename`, sha256sum format |
| `release-manifest.json` | source commit + dirty flag, component versions, bootstrap pins, per-artifact bytes/sha256 and per-file sha256 |
| `docs/` | `INSTALL.md`, `RELEASE.md` |

The build is deterministic: fixed zip timestamps (2026-01-01), sorted entries, fixed
mode bits, no build clock. Building the same commit twice must produce identical bytes.

## 3. Verify

```bash
# 1. build twice and compare
python3 scripts/build_release.py --version 0.2.0 --out /tmp/rel-a
python3 scripts/build_release.py --version 0.2.0 --out /tmp/rel-b
diff -r /tmp/rel-a/v0.2.0 /tmp/rel-b/v0.2.0 && echo "deterministic: OK"

# 2. hashes + manifest
cd dist/v0.2.0 && shasum -a 256 -c SHA256SUMS
python3 - <<'PY'
import json, hashlib, pathlib
m = json.load(open("release-manifest.json"))
for a in m["artifacts"]:
    h = hashlib.sha256(pathlib.Path(a["name"]).read_bytes()).hexdigest()
    assert h == a["sha256"], a["name"]
print("manifest hashes: OK", m["release"], m["source"])
PY

# 3. archive shape
unzip -l wowhermes-plugin-v0.2.0.zip | head -20     # must start with wow/
unzip -l WoWHermes-v0.2.0.zip | grep media/         # icon art present
unzip -l wowhermes-bridge-v0.2.0.zip | grep uv.lock # --frozen needs the lock

# 4. repository tests (builder invariants, secrets/noise scan, PowerShell static checks)
python3 -m unittest discover -s scripts/tests -v
python3 scripts/package_addon.py --check-only

# 5. bridge and plugin gates (unchanged by release work, run them anyway)
(cd bridge && uv run pytest -q)
```

On Windows, re-verify every artifact hash (the release notes publish `SHA256SUMS`):

```powershell
Get-FileHash .\wowhermes-bridge-v0.2.0.zip -Algorithm SHA256
```

## 4. Windows bootstrap proof (sandbox only)

Run against an extracted zip in a throwaway directory, with a scratch WoW tree and a
scratch install root. Not against the live game, the live config, or the live task.

```powershell
# scratch WoW tree: anything with WTF\Account\TEST and Interface\AddOns
$sandbox = "$env:LOCALAPPDATA\wowhermes-bootstrap-test"
New-Item -ItemType Directory -Force "$sandbox\wow\_forever_\WTF\Account\TEST", "$sandbox\wow\_forever_\Interface\AddOns" | Out-Null
Expand-Archive .\wowhermes-bridge-v0.2.0.zip -DestinationPath "$sandbox\zip" -Force

# 1. plan only
powershell -NoProfile -ExecutionPolicy Bypass -File "$sandbox\zip\wowhermes-bridge-v0.2.0\bootstrap\install.ps1" `
  -WowDir "$sandbox\wow\_forever_" -InstallDir "$sandbox\home" -GatewayUrl http://127.0.0.1:8791 -DryRun

# 2. toolchain proof: uv download + SHA256, managed Python 3.12, uv sync --frozen --no-dev
$t = ConvertTo-SecureString 'sandbox-dummy-token-not-a-real-secret' -AsPlainText -Force
powershell -NoProfile -ExecutionPolicy Bypass -File "$sandbox\zip\wowhermes-bridge-v0.2.0\bootstrap\install.ps1" `
  -WowDir "$sandbox\wow\_forever_" -InstallDir "$sandbox\home" -GatewayUrl http://127.0.0.1:8791 `
  -Token $t -DryRun -ProvisionTools

# 3. full install into the sandbox (no scheduled task, no autostart, dummy token)
powershell -NoProfile -ExecutionPolicy Bypass -File "$sandbox\zip\wowhermes-bridge-v0.2.0\bootstrap\install.ps1" `
  -WowDir "$sandbox\wow\_forever_" -InstallDir "$sandbox\home" -GatewayUrl http://127.0.0.1:8791 `
  -Token $t -NoAutostart
Get-Content "$sandbox\home\config.toml" | Select-String -NotMatch 'token'
& "$sandbox\home\venv\Scripts\wowhermes-bridge.exe" --version
& "$sandbox\home\venv\Scripts\wowhermes-bridge.exe" status

# 4. idempotency: run step 3 again, expect "kept" not "created" for unchanged files
# 5. uninstall: remove the sandbox only
powershell -NoProfile -ExecutionPolicy Bypass -File "$sandbox\zip\wowhermes-bridge-v0.2.0\bootstrap\uninstall.ps1" `
  -InstallDir "$sandbox\home" -WowDir "$sandbox\wow\_forever_" -RemoveAddon -Purge
Remove-Item "$sandbox" -Recurse -Force
```

Confirmed evidence (commands + observed output) belongs in `docs/notes/ws-release.md`,
not in this document.

## 5. Release checklist

- [ ] `plugin.yaml`, TOC, `pyproject.toml`, `CHANGELOG.md` updated and committed.
- [ ] `python3 scripts/build_release.py --version <v>` run from a clean tree (manifest
      `source.dirty == false`).
- [ ] Two builds byte-identical; `shasum -a 256 -c SHA256SUMS` passes.
- [ ] Zip contents inspected: plugin rooted at `wow/`, addon has `media/`, bridge has
      `uv.lock` + `bootstrap/`, no `tests/`, `__pycache__`, `.venv`, `.pytest_cache`.
- [ ] `release-manifest.json` component versions match the sources; bootstrap pins match
      `install.ps1`.
- [ ] `python3 -m unittest discover -s scripts/tests -v` green (includes the secret /
      private-IP / user-path scan of the artifacts).
- [ ] Windows sandbox proof recorded (dry run, toolchain, full install, idempotent
      re-run, uninstall) with real output.
- [ ] `LICENSE` + `NOTICE` present at the repo root and inside the bridge zip.
- [ ] Installer help text and `docs/INSTALL.md` agree on flags and paths.
- [ ] No token, password, private hostname/IP or personal path in any artifact.
- [ ] Tag `v<version>`; publish the three zips, `SHA256SUMS`, `release-manifest.json`,
      `LICENSE`, `NOTICE`; upload `WoWHermes-v<version>.zip` to CurseForge/Wago and the
      bridge zip to the release page with the "companion required" note.

## 6. Publishing (not performed by the build)

Publishing is manual and always after the checklist is green:

```bash
git tag -s v0.2.0 -m "WoWHermes v0.2.0"
git push gitlab v0.2.0
# then upload dist/v0.2.0/* to the GitLab release / CurseForge project
```

Do not re-zip by hand after the hash list is published: any change invalidates
`SHA256SUMS` and the manifest.

## 7. Rollback

Re-publish the previous release's three zips and `SHA256SUMS`. On the game PC, extract
the older bridge zip and re-run its `install.ps1` (idempotent, keeps the token); in game,
`/reload`. On the Hermes host, replace `plugins/wow/` with the older
`wowhermes-plugin-v<old>.zip` contents and restart the gateway.
