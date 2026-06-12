# media_utils

Personal scripts for video post-processing and media management.

```bash
pip install -r requirements.txt
```

## Repository layout

```
src/        Python source files
scripts/    Windows .cmd launchers (double-click or call from PATH)
tests/      pytest test suite
```

---

## mediactl — Background supervisor

System-tray app that runs `render_vr360` and `sync_media_to_s3` persistently in the
background, sends Windows toast notifications on key events, and provides a per-script
log viewer and status dashboard.

### Quick start

Double-click `scripts/mediactl.cmd`, or run it from a terminal. The console window
closes immediately — mediactl runs silently as a tray icon.

**Left-click** the tray icon to open the status dashboard. **Right-click** for the full menu.

### Verifying it started

Check `mediactl.log` in the repo root:

```
type mediactl.log
```

Per-worker stdout is also written to `logs/`:

```
logs/render_vr360.log
logs/s3_sync.log
```

Key lines to look for:

| Line | Meaning |
|---|---|
| `mediactl starting` | Process launched |
| `Worker X started (pid …)` | Each worker is running |
| `Tray icon ready and visible` | Tray icon appeared |
| `NOTIFY … \| …` | A notification was dispatched |

### Single-instance

Running `mediactl.cmd` a second time exits immediately — a Windows named mutex
(`Global\mediactl_singleton`) ensures only one instance runs at a time.

### Tray icon

Look for the icon in the system tray (bottom-right). If it is hidden, click the `^`
arrow to reveal hidden icons.

Right-click menu:

| Item | Action |
|---|---|
| **Show status** | Opens the status dashboard (same as left-click) |
| **Render VR360 › Stop/Start/Restart** | Control that worker |
| **S3 Sync › Stop/Start/Restart** | Control that worker |
| **Launch at startup** | Toggle Windows autostart (see below) |
| **Quit** | Stop all workers and exit |

### Status dashboard

Left-click the tray icon (or *Show status* in the menu) to open a window with:

- Per-worker status (running/stopped, pid, uptime)
- Start / Stop / Restart buttons per worker
- Live log tail per worker
- Launch at startup checkbox and Quit button

### Launch at startup

*Right-click tray → Launch at startup* writes or removes a value under:

```
HKCU\Software\Microsoft\Windows\CurrentVersion\Run
```

Value name: `mediactl`  
Value: `"<path\to\pythonw.exe>" "<path\to\src\mediactl.py>"`

Windows reads this key at every user login and runs the command automatically.
The menu item shows a checkmark when the entry is present.

### Auto-restart

If a worker script exits unexpectedly (any non-zero code) it is restarted
automatically after 5 seconds.

### Adding a new background script

Add one entry to `WORKERS` in `src/mediactl.py`:

```python
{
    "name": "My Script",
    "cmd": [PYTHON, str(THIS_DIR / "my_script.py")],
    "cwd": THIS_DIR,
    "notifications": [
        {"prefix": "NOTIFY:Job done", "title": "My Script finished"},
    ],
}
```

Print `NOTIFY:title|body` anywhere in the script to trigger a toast notification.

### Dependencies

| Package | Purpose |
|---|---|
| `pystray` | System tray icon |
| `Pillow` | Icon rendering |

---

## render_vr360 — VAM frame sequence encoder

Assembles a [VAM Video Renderer](https://hub.virtamate.com/resources/video-renderer-for-3d-vr180-vr360-and-flat-2d-audio-bvh-animation-recorder.11994/)
frame sequence (PNG or JPG + WAV) into an MP4. Injects 360 spherical metadata for VR
outputs. Runs as a watch loop by default — mediactl starts it automatically.

### Dependencies

| Tool | Purpose | Install |
|---|---|---|
| [FFmpeg](https://ffmpeg.org) | Encoding | `winget install Gyan.FFmpeg` |
| [exiftool](https://exiftool.org) | 360 metadata injection | Download zip, rename to `exiftool.exe`, place in `~/bin` |
| Python 3.10+ | Script runtime | `winget install Python.Python.3` |

### Usage

```bash
# Watch loop (default) — polls every 10 s, renders all pending folders automatically
scripts\render_vr360.cmd

# Render all pending folders once and exit
scripts\render_vr360.cmd --once

# Render a specific folder
scripts\render_vr360.cmd "C:\Games\Vam\Saves\VR_Renders\20260607-002259"

# Re-render already-completed folders
scripts\render_vr360.cmd --force
```

### Options

| Flag | Default | Description |
|---|---|---|
| `source` | auto | Folder to render; default scans `C:\Games\Vam\Saves\VR_Renders` for pending folders |
| `-r`, `--framerate` | auto | Output fps; auto-detected from frame count / audio duration |
| `--crf` | `20` | libx265 quality (0–51, lower = better) |
| `--cq` | `20` | hevc_nvenc quality (0–51, lower = better) |
| `--stereo` | `mono` | `mono`, `left-right`, or `top-bottom` |
| `--output-name` | folder name | Base name for output file |
| `--audio-offset` | `-0.3` | AV sync offset in seconds (negative = video leads audio) |
| `--flat` | off | Force flat output (skip VR metadata) |
| `--force` | off | Overwrite existing output and ignore completion markers |
| `--once` | off | Render pending folders once and exit instead of watching |
| `--interval` | `10` | Watch poll interval in seconds |

### Output

`C:\Movies\V Vam My Render\{name} {mode} {res} {fps}.mp4`

Examples:
- `20260608-014702 360mono 6k 60fps.mp4` — VR360, source ≥ 6k
- `20260608-014702 flat 4k 30fps.mp4` — flat, source < 6k (auto-detected)

### Auto-flat

Sources below 6k (`max(width, height) < 5760`) are automatically rendered without VR
metadata. Use `--flat` to force flat on any resolution.

### Completion markers

On success, `_RENDER_COMPLETE_.txt` is written into the source folder containing the
output path and file size. This prevents re-rendering on the next watch cycle. Delete
the marker or use `--force` to re-render.

### Encoder strategy

1. **hevc_nvenc** (NVIDIA GPU) — tried first, ~50% faster
2. **libx265** (CPU) — automatic fallback

---

## sync_media_to_s3 — S3 media sync

Watches local movie folders and uploads new or changed files to S3, preserving relative
paths. Configured for `C:\Movies\` and `E:\Movies\` → `s3://park.movies.archive/`.

### Dependencies

| Tool | Purpose | Install |
|---|---|---|
| [watchdog](https://pypi.org/project/watchdog/) | Filesystem events | `pip install watchdog` |
| [AWS CLI](https://aws.amazon.com/cli/) | S3 upload | `winget install Amazon.AWSCLI` + `aws configure` |

### Usage

```bash
# Start watcher (initial sync + watch)
scripts\sync_media_to_s3.cmd

# Skip initial sync
scripts\sync_media_to_s3.cmd --no-initial-sync

# Preview without uploading
scripts\sync_media_to_s3.cmd --dry-run
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--no-initial-sync` | off | Skip `aws s3 sync` on startup |
| `--stable-secs N` | `60` | Seconds of unchanged file size before upload |
| `--dry-run` | off | Print commands without executing |

### Upload guards

1. **Temp extensions** — `.part`, `.crdownload`, `.!qb`, `.tmp`, `.download` are never uploaded
2. **Stability check** — file must have the same size for `--stable-secs` seconds before upload
3. **Ignore file** — patterns in `src/sync_media_to_s3.ignore` skip matching paths (glob `*`/`?`, or `re:` prefix for regex)
