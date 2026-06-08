# media_utils

Personal scripts for video post-processing.

---

## render_vr360.py

Assembles a [VAM Video Renderer](https://hub.virtamate.com/resources/video-renderer-for-3d-vr180-vr360-and-flat-2d-audio-bvh-animation-recorder.11994/) frame sequence (PNG or JPG + WAV) into a 360 monoscopic VR video with proper spherical metadata.

### Dependencies

| Tool | Purpose | Install |
|---|---|---|
| [FFmpeg](https://ffmpeg.org) | Encoding | `winget install Gyan.FFmpeg` |
| [exiftool](https://exiftool.org) | 360 metadata injection | Download zip, rename to `exiftool.exe`, place in `~/bin` |
| Python 3.10+ | Script runtime | `winget install Python.Python.3` |

### PATH setup

`C:\Users\parku\repos\media_utils` is on your user PATH. Open a **new terminal** after setup, then run from anywhere:

```bash
render_vr360 "C:\Games\Vam\Saves\VR_Renders\20260607-002259"
```

The launcher is `render_vr360.cmd` (calls `python render_vr360.py`).

### Usage

```bash
# Basic — auto-detects frames, audio, framerate, names output after folder
render_vr360 "C:\Games\Vam\Saves\VR_Renders\20260607-002259"

# Custom framerate override
render_vr360 .\my_render -r 30

# Scale down to 4K (faster encode / broader player support)
render_vr360 .\my_render --resolution 3840x1920
```

### Options

| Flag | Default | Description |
|---|---|---|
| `source` | `.` | Folder with frame sequence and WAV |
| `-r`, `--framerate` | auto | Output framerate; auto-detected from frame count / audio duration (must be ~30 or ~60 fps) |
| `--crf` | `20` | libx265 quality (0–51, lower = better) |
| `--cq` | `20` | hevc_nvenc quality (0–51, lower = better) |
| `--resolution` | source res | Scale output, e.g. `3840x1920` |
| `--stereo` | `mono` | `mono`, `left-right`, or `top-bottom` |
| `--output-name` | folder name | Base name for output file |

### Output

`{source}/rendered/{name}_360mono.mp4` (or `_360left-right.mp4` etc.)

### Encoder strategy

1. **hevc_nvenc** (NVIDIA GPU) — tried first, typically ~50% faster
2. **libx265** (CPU) — automatic fallback if GPU encoding fails

Both use quality-based rate control (CQ/CRF 20) per the VAM plugin's recommendation.
A `format=yuv420p` filter is prepended to fix the RGB→YUV conversion issue that causes NVENC to stall on 8K PNG input.

### Spherical metadata

Injects Google Spatial Media–compatible XMP tags recognized by YouTube, DeoVR, VirtualDesktop, Meta Quest, and most other 360 players:

```
ProjectionType = equirectangular
Spherical      = true
Stitched       = true
StereoMode     = mono
```
