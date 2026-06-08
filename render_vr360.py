#!/usr/bin/env python3
"""
render_vr360.py — Assemble a VAM Video Renderer frame sequence into a
360 monoscopic VR video with spherical metadata.

Encoder strategy:
  1. hevc_nvenc (NVIDIA GPU) — faster
  2. libx265 (CPU)          — automatic fallback

Dependencies: ffmpeg, ffprobe, exiftool (all must be on PATH)
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path


# ── Logging helpers ───────────────────────────────────────────────────────────

def info(msg: str) -> None:
    print(f"  {msg}")

def step(msg: str) -> None:
    print(f"\n[*] {msg}")

def ok(msg: str) -> None:
    print(f"  -> {msg}")

def warn(msg: str) -> None:
    print(f"  [!] {msg}", file=sys.stderr)

def die(msg: str) -> None:
    print(f"\n[FAIL] {msg}", file=sys.stderr)
    sys.exit(1)


# ── Frame/audio discovery ─────────────────────────────────────────────────────

def find_frames(source: Path) -> tuple[list[Path], str]:
    """Return sorted frame list and extension (png or jpg)."""
    for ext in ("png", "jpg"):
        frames = sorted(source.glob(f"*_??????.{ext}"))
        if frames:
            return frames, ext
    die(f"No PNG or JPG frame sequence found in: {source}")


def frame_pattern(frames: list[Path], ext: str) -> Path:
    """Derive ffmpeg input pattern from first frame path."""
    base = re.sub(r"_\d{6}$", "", frames[0].stem)
    return frames[0].parent / f"{base}_%06d.{ext}"


def find_audio(source: Path) -> Path | None:
    wavs = sorted(source.glob("*.wav"))
    return wavs[-1] if wavs else None


# ── ffprobe helper ────────────────────────────────────────────────────────────

def probe_resolution(frame: Path) -> tuple[int, int]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(frame)],
        capture_output=True, text=True
    )
    w, h = result.stdout.strip().split(",")
    return int(w), int(h)


# ── ffmpeg with live progress ─────────────────────────────────────────────────

def run_ffmpeg(args: list[str], total_frames: int, label: str) -> bool:
    """
    Run ffmpeg, parsing -progress output to show a single updating line.
    Returns True on success.
    """
    # -progress writes key=value pairs to fd 1; redirect stderr to suppress noise
    full_args = ["ffmpeg"] + args + ["-progress", "pipe:2", "-loglevel", "error", "-stats_period", "0.5"]

    proc = subprocess.Popen(full_args, stderr=subprocess.PIPE, text=True, bufsize=1)

    frame = 0
    fps = 0.0
    speed = ""

    for line in proc.stderr:
        line = line.strip()
        if line.startswith("frame="):
            frame = int(line.split("=", 1)[1])
        elif line.startswith("fps="):
            try:
                fps = float(line.split("=", 1)[1])
            except ValueError:
                pass
        elif line.startswith("speed="):
            speed = line.split("=", 1)[1]
        elif line == "progress=end":
            break
        elif "=" not in line and line:
            # Non-progress line means an error message
            print(f"\n  ffmpeg: {line}", file=sys.stderr)

        if total_frames > 0 and fps > 0:
            pct = min(frame / total_frames * 100, 100)
            eta_s = (total_frames - frame) / fps if fps > 0 else 0
            eta = f"{int(eta_s // 60):02d}:{int(eta_s % 60):02d}"
            bar_w = 25
            filled = int(bar_w * pct / 100)
            bar = "#" * filled + "-" * (bar_w - filled)
            print(
                f"\r  {label}  [{bar}] {pct:5.1f}%  "
                f"frame {frame}/{total_frames}  {fps:.1f} fps  ETA {eta}   ",
                end="", flush=True
            )

    proc.wait()
    print()  # newline after progress line
    return proc.returncode == 0


# ── exiftool metadata injection ───────────────────────────────────────────────

def inject_metadata(mp4: Path, stereo_mode: str) -> None:
    step("Injecting 360 spherical metadata")
    subprocess.run(
        [
            "exiftool",
            "-XMP-GSpherical:Spherical=true",
            "-XMP-GSpherical:Stitched=true",
            "-XMP-GSpherical:ProjectionType=equirectangular",
            f"-XMP-GSpherical:StereoMode={stereo_mode}",
            "-overwrite_original",
            str(mp4),
        ],
        check=True, capture_output=True
    )
    ok(f"equirectangular / {stereo_mode}")


# ── Encoding ──────────────────────────────────────────────────────────────────

def build_common_args(
    pattern: Path,
    audio: Path | None,
    framerate: int,
    resolution: str | None,
    output: Path,
) -> list[str]:
    vf_parts = ["format=yuv420p"]
    if resolution:
        vf_parts.insert(0, f"scale={resolution}")

    args = [
        "-y",
        "-framerate", str(framerate),
        "-i", str(pattern),
    ]
    if audio:
        args += ["-i", str(audio), "-c:a", "aac", "-b:a", "192k"]
    else:
        args += ["-an"]

    args += [
        "-vf", ",".join(vf_parts),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]
    return args


def encode(
    pattern: Path,
    audio: Path | None,
    total_frames: int,
    framerate: int,
    resolution: str | None,
    crf: int,
    cq: int,
    output: Path,
) -> str:
    """Try hevc_nvenc, fall back to libx265. Returns encoder name used."""
    common = build_common_args(pattern, audio, framerate, resolution, output)

    step("Encoding with hevc_nvenc (GPU)")
    nvenc_args = common + ["-c:v", "hevc_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", str(cq), str(output)]
    if run_ffmpeg(nvenc_args, total_frames, "hevc_nvenc"):
        if output.exists() and output.stat().st_size > 0:
            return "hevc_nvenc"

    warn("hevc_nvenc failed — falling back to libx265 (CPU)")
    output.unlink(missing_ok=True)

    step("Encoding with libx265 (CPU)")
    x265_args = common + ["-c:v", "libx265", "-tune:v", "fastdecode", "-level:v", "6.2", "-crf", str(crf), str(output)]
    if not run_ffmpeg(x265_args, total_frames, "libx265  "):
        die("Encoding failed with both hevc_nvenc and libx265.")

    return "libx265"


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Assemble VAM frame sequence into a 360 VR video."
    )
    parser.add_argument("source", nargs="?", default=".",
                        help="Folder with frame sequence and WAV (default: current dir)")
    parser.add_argument("-r", "--framerate", type=int, default=60)
    parser.add_argument("--crf", type=int, default=20, help="libx265 quality (0–51, lower=better)")
    parser.add_argument("--cq",  type=int, default=20, help="hevc_nvenc quality (0–51, lower=better)")
    parser.add_argument("--resolution", help="Scale output, e.g. 3840x1920")
    parser.add_argument("--stereo", default="mono",
                        choices=["mono", "left-right", "top-bottom"])
    parser.add_argument("--output-name", help="Base name for output file (default: folder name)")
    args = parser.parse_args()

    # Dependency checks
    for tool in ("ffmpeg", "ffprobe", "exiftool"):
        if not shutil.which(tool):
            die(f"'{tool}' not found on PATH. Please install it first.")

    source = Path(args.source).resolve()
    if not source.is_dir():
        die(f"Source folder not found: {source}")

    output_name = args.output_name or source.name
    suffix = f"360{args.stereo}"
    output_dir = source / "rendered"
    output_dir.mkdir(exist_ok=True)
    output = output_dir / f"{output_name}_{suffix}.mp4"

    print(f"\n" + "=" * 55)
    print(f"  VAM VR360 Renderer")
    print("=" * 55)
    info(f"Source  : {source}")
    info(f"Output  : {output}")

    # Discover inputs
    frames, ext = find_frames(source)
    pattern = frame_pattern(frames, ext)
    audio = find_audio(source)
    w, h = probe_resolution(frames[0])

    info(f"Frames  : {len(frames)} x .{ext}  ({w}x{h})  @ {args.framerate} fps")
    info(f"Audio   : {audio.name if audio else 'none'}")
    if args.resolution:
        info(f"Scale   : {args.resolution}")

    # Encode
    import time
    t0 = time.perf_counter()
    encoder = encode(pattern, audio, len(frames), args.framerate, args.resolution, args.crf, args.cq, output)
    elapsed = time.perf_counter() - t0

    enc_fps = len(frames) / elapsed if elapsed > 0 else 0
    size_mb = output.stat().st_size / 1_048_576
    ok(f"{encoder}  |  {enc_fps:.1f} encode-fps  |  {elapsed:.1f}s  |  {size_mb:.2f} MB")

    # Metadata
    inject_metadata(output, args.stereo)

    print("\n" + "=" * 55)
    print(f"  Done: {output}")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    main()
