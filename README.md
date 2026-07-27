# media_utils

Personal scripts for video post-processing and media management.

```bash
pip install -r requirements.txt
```

Layout:

```
config/           Per-OS sync.toml + ignore rules (in-repo)
src/sync/         S3 folder watcher (cross-platform)
src/supervisor/   Tray supervisor — mediactl (Win + Mac)
src/windows/      Windows-only: render_vr360, concat_videos
scripts/windows/  .cmd / .ps1 launchers
scripts/macos/    shell launchers
tests/            pytest suite
```

---

## Sync config

Per-OS files live in the repo under `config/`:

| OS | Sync config | Ignore file |
|----|-------------|-------------|
| macOS | `config/sync.macos.toml` | `config/sync.macos.ignore` |
| Windows | `config/sync.windows.toml` | `config/sync.windows.ignore` |

Runtime state (lock/socket) stays in `~/.config/media_utils/`.

macOS (`config/sync.macos.toml`):

```toml
[[sources]]
path = "~/Movies"
s3 = "s3://park.movies.archive/"
exclude = ["TV", "iTubeGo"]
```

`exclude` skips those top-level folders (and everything under them) for both the watcher and `aws s3 sync`. Also supported as a top-level `exclude = [...]`.

Windows (`config/sync.windows.toml`) keeps `C:/Movies`, `E:/Movies` → movies archive and `E:/Pics` → photos archive.

---

## macOS

### Setup

```bash
uv venv --python 3.11 .venv
uv pip install -r requirements.txt
# AWS CLI configured (`aws configure`)
bash scripts/macos/setup_path.sh   # optional PATH
```

Launchers prefer `.venv/bin/python` when present.

### Run sync alone

```bash
scripts/macos/sync_media_to_s3 --dry-run --no-initial-sync
# or: PYTHONPATH=src python3 -m sync
```

### mediactl (menu bar supervisor)

Supervises **S3 Sync only**.

```bash
scripts/macos/mediactl
```

- Menu bar icon → Show status / Start Stop Restart / Launch at startup / Quit
- **Left-click** tray icon → status/control window; **right-click** → menu
- Status window is a separate process (Tk cannot share the tray process on macOS) with Start/Stop/Restart, Launch at startup, Quit via Unix socket (`~/.config/media_utils/mediactl.sock`)
- Autostart: LaunchAgent `~/Library/LaunchAgents/com.media_utils.mediactl.plist` (`KeepAlive`)

### Logs (self-debug)

| Log | Path | What to look for |
|---|---|---|
| Supervisor | `mediactl.log` (repo root) | `mediactl starting`, `Control socket listening`, `Tray icon ready`, `Worker … started/stopped`, `Control request: …`, `Opened status viewer` |
| S3 Sync worker | `logs/s3_sync.log` | `Config:`, `Configured … → s3://…`, `watching`, `upload:`, `NOTIFY:`, aws errors |
| Status window | `logs/status_viewer.log` | `Status viewer starting`, `UI start/stop/restart`, connect failures |
| LaunchAgent | `~/Library/Logs/mediactl/launchd.out.log` / `.err.log` | crash stacks if the agent dies at login |

Quick check:

```bash
tail -50 ~/repos/media_utils/mediactl.log
tail -50 ~/repos/media_utils/logs/s3_sync.log
pgrep -fl 'python -m supervisor|python -m sync'
launchctl list | grep media_utils
ls -la ~/.config/media_utils/
```

---

## Windows

### Setup

```powershell
pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File scripts\windows\setup_path.ps1
powershell -ExecutionPolicy Bypass -File scripts\windows\create_desktop_shortcut.ps1
```

### Tools

| Launcher | Role |
|---|---|
| `mediactl` | Tray supervisor: **Render VR360** + **S3 Sync** |
| `sync_media_to_s3` | S3 watcher alone |
| `render_vr360` | VAM frame sequence → VR/flat MP4 |
| `concat_videos` | Lossless concat + VR metadata restore |

See prior docs for render_vr360 / concat_videos flags (unchanged behavior).

---

## Tests

```bash
cd /path/to/media_utils
pip install -r requirements.txt
pytest -q
```
