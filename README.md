# media_utils

Personal scripts for video post-processing and media management.

```
pip install -r requirements.txt
```

---

## mediactl.py — Background supervisor

System-tray app that runs `render_vr360` and `sync_media_to_s3` persistently in the background, sends Windows toast notifications on key events, and provides a per-script log viewer.

### Dependencies

| Package | Install |
|---|---|
| `pystray` | `pip install pystray` |
| `Pillow` | `pip install Pillow` |

### Usage

```bash
mediactl          # starts tray app (no console window)
```

Or double-click `mediactl.cmd`.

### Features

- **System tray icon** — right-click menu for all controls
- **Toast notifications** — render started/complete/failed, S3 upload queued/complete, warnings
- **Log window** — dark tabbed panel, one scrolling log per script, Restart buttons
- **Auto-restart** — if a script crashes it restarts automatically after 5 s
- **Launch at startup** — right-click tray → *Launch at startup* writes/removes a Windows registry Run key

### Adding a new script

Add one entry to `WORKERS` in `mediactl.py`:

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

Print `NOTIFY:title|body` anywhere in the script to trigger a toast.

---

## render_vr360.py — VAM frame sequence encoder

Assembles a [VAM Video Renderer](https://hub.virtamate.com/resources/video-renderer-for-3d-vr180-vr360-and-flat-2d-audio-bvh-animation-recorder.11994/) frame sequence (PNG or JPG + WAV) into an MP4. Injects 360 spherical metadata for VR outputs. Runs as a watch loop by default.

### Dependencies

| Tool | Purpose | Install |
|---|---|---|
| [FFmpeg](https://ffmpeg.org) | Encoding | `winget install Gyan.FFmpeg` |
| [exiftool](https://exiftool.org) | 360 metadata injection | Download zip, rename to `exiftool.exe`, place in `~/bin` |
| Python 3.10+ | Script runtime | `winget install Python.Python.3` |

### Usage

```bash
# Watch loop (default) — polls every 10s, renders all pending folders automatically
render_vr360

# Render all pending folders once and exit
render_vr360 --once

# Render a specific folder
render_vr360 "C:\Games\Vam\Saves\VR_Renders\20260607-002259"

# Re-render already-completed folders
render_vr360 --force
```

### Options

| Flag | Default | Description |
|---|---|---|
| `source` | auto | Folder to render; default scans `C:\Games\Vam\Saves\VR_Renders` for pending folders |
| `-r`, `--framerate` | auto | Output fps; auto-detected from frame count / audio duration (must be ~30 or ~60) |
| `--crf` | `20` | libx265 quality (0–51, lower = better) |
| `--cq` | `20` | hevc_nvenc quality (0–51, lower = better) |
| `--stereo` | `mono` | `mono`, `left-right`, or `top-bottom` |
| `--output-name` | folder name | Base name for output file |
| `--audio-offset` | `-0.3` | AV sync offset in seconds (negative = video leads audio) |
| `--flat` | off | Force flat output even on 6k+ sources (skip VR metadata) |
| `--force` | off | Overwrite existing output and ignore completion markers |
| `--once` | off | Render pending folders once and exit instead of watching |
| `--interval` | `10` | Watch poll interval in seconds |

### Output

`C:\Movies\V Vam My Render\{name} {mode} {res} {fps}.mp4`

Examples:
- `20260608-014702 360mono 6k 60fps.mp4` — VR360, source ≥ 6k
- `20260608-014702 4k 30fps.mp4` — flat, source < 6k (auto-detected)

### Auto-flat

Sources below 6k (`max(width, height) < 5760`) are automatically rendered without VR metadata. Use `--flat` to force flat on any resolution.

### Completion markers

On success, `_RENDER_COMPLETE_.txt` is written into the source folder containing the output path and file size. This prevents re-rendering on the next watch cycle. Delete the marker or use `--force` to re-render.

### Encoder strategy

1. **hevc_nvenc** (NVIDIA GPU) — tried first, ~50% faster
2. **libx265** (CPU) — automatic fallback

### Spherical metadata

Injects Google Spatial Media–compatible XMP tags for VR outputs:

```
ProjectionType = equirectangular
Spherical      = true
Stitched       = true
StereoMode     = mono
```

---

## sync_media_to_s3.py — S3 media sync

Watches local movie folders and uploads new or changed files to S3, preserving relative paths. Configured for `C:\Movies\` and `E:\Movies\` → `s3://park.movies.archive/`.

### Dependencies

| Tool | Purpose | Install |
|---|---|---|
| [watchdog](https://pypi.org/project/watchdog/) | Filesystem events | `pip install watchdog` |
| [AWS CLI](https://aws.amazon.com/cli/) | S3 upload | `winget install Amazon.AWSCLI` + `aws configure` |

### Usage

```bash
# Start watcher (initial sync + watch)
sync_media_to_s3

# Skip initial sync
sync_media_to_s3 --no-initial-sync

# Preview without uploading
sync_media_to_s3 --dry-run

# Custom stability window (default 60s)
sync_media_to_s3 --stable-secs 120
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--no-initial-sync` | off | Skip `aws s3 sync` on startup |
| `--stable-secs N` | `60` | Seconds of unchanged file size before upload |
| `--dry-run` | off | Print commands without executing |

### Upload guards

1. **Temp extensions** — `.part`, `.crdownload`, `.!qb`, `.tmp`, `.download` are never uploaded
2. **Stability check** — file must have the same size for `--stable-secs` seconds before upload (prevents syncing active renders or downloads)
3. **Ignore file** — patterns in [`sync_media_to_s3.ignore`](sync_media_to_s3.ignore) skip matching paths (glob `*`/`?`, or `re:` prefix for regex)
