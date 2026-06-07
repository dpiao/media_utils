# media_utils

Personal scripts for video post-processing.

---

## Render-VR360.ps1

Assembles a [VAM Video Renderer](https://hub.virtamate.com/resources/video-renderer-for-3d-vr180-vr360-and-flat-2d-audio-bvh-animation-recorder.11994/) frame sequence (PNG or JPG + WAV) into a 360 monoscopic VR video with proper spherical metadata.

### Dependencies

| Tool | Purpose | Install |
|---|---|---|
| [FFmpeg](https://ffmpeg.org) | Encoding | `winget install Gyan.FFmpeg` |
| [exiftool](https://exiftool.org) | 360 metadata injection | Download from exiftool.org, rename to `exiftool.exe`, add to PATH |

### Usage

```powershell
# Basic — auto-detects frames, audio, names output after folder
.\Render-VR360.ps1 -SourceFolder "C:\Games\Vam\Saves\VR_Renders\20260607-002259"

# Custom framerate and explicit exiftool path
.\Render-VR360.ps1 -SourceFolder ".\my_render" -Framerate 30 -ExifToolPath "C:\tools\exiftool.exe"

# Scale down to 4K output (e.g. for faster encode or player compatibility)
.\Render-VR360.ps1 -SourceFolder ".\my_render" -Resolution 3840x1920
```

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `-SourceFolder` | `.` | Folder with frame sequence and WAV |
| `-Framerate` | `60` | Output video framerate |
| `-Crf` | `20` | libx265 quality (lower = better, 0–51) |
| `-Cq` | `20` | hevc_nvenc quality (lower = better, 0–51) |
| `-Resolution` | source | Scale output, e.g. `3840x1920` |
| `-StereoMode` | `mono` | `mono`, `left-right`, or `top-bottom` |
| `-OutputName` | folder name | Base name for output file |
| `-ExifToolPath` | auto | Path to `exiftool.exe` |

### Output

A file named `{OutputName}_360mono.mp4` (or `_360left-right.mp4` etc.) is created inside a `rendered/` subfolder of the source folder.

### Encoder strategy

1. Tries **hevc_nvenc** (NVIDIA GPU) first — typically ~50% faster
2. Falls back to **libx265** (CPU) automatically if GPU encoding fails
3. Both use quality-based rate control (CRF/CQ 20) per the VAM plugin's recommendation
4. Prepends `format=yuv420p` in the filter chain to fix the RGB→YUV conversion issue with NVENC and 8K PNG input

### Spherical metadata

Injects Google Spatial Media–compatible XMP tags:
- `ProjectionType = equirectangular`
- `Spherical = true`
- `Stitched = true`
- `StereoMode = mono` (or as specified)

Recognized by YouTube, DeoVR, VirtualDesktop, Meta Quest video players, and most other 360 players.
