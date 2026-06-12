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
import time
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


DEFAULT_VR_RENDERS = Path(r"C:\Games\Vam\Saves\VR_Renders")
DEFAULT_OUTPUT_DIR = Path(r"C:\Movies\V Vam My Render")
RENDER_COMPLETE_MARKER = "_RENDER_COMPLETE_.txt"
DEFAULT_WATCH_INTERVAL = 10
SPINNER_TICK_SEC = 0.3  # spinner frame interval (3x slower than 0.1s)


class RenderError(Exception):
    pass


def has_frame_sequence(folder: Path) -> bool:
    return any(folder.glob("*_??????.png")) or any(folder.glob("*_??????.jpg"))


def is_render_complete(folder: Path) -> bool:
    return (folder / RENDER_COMPLETE_MARKER).is_file()


def find_pending_folders(renders_root: Path = DEFAULT_VR_RENDERS, force: bool = False) -> list[Path]:
    """Return finished capture folders (frames + WAV) not yet rendered."""
    if not renders_root.is_dir():
        die(f"VR renders folder not found: {renders_root}")

    pending: list[Path] = []
    for folder in renders_root.iterdir():
        if not folder.is_dir():
            continue
        if not has_frame_sequence(folder):
            continue
        if not is_export_complete(folder):
            continue
        if is_render_complete(folder) and not force:
            continue
        pending.append(folder)

    return sorted(pending, key=lambda p: p.name)


def write_render_complete(source: Path, output: Path) -> None:
    marker = source / RENDER_COMPLETE_MARKER
    size = output.stat().st_size
    marker.write_text(
        f"directory: {output.parent}\n"
        f"file name: {output.name}\n"
        f"file size: {size}\n",
        encoding="utf-8",
    )
    ok(f"Marked complete: {marker}")


def exit_notice(msg: str) -> None:
    print(f"\n{msg}\n")
    sys.exit(0)


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


def is_export_complete(folder: Path) -> bool:
    """VAM writes the WAV when the capture export has finished."""
    return find_audio(folder) is not None


# ── ffprobe helper ────────────────────────────────────────────────────────────

def probe_resolution(frame: Path) -> tuple[int, int]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(frame)],
        capture_output=True, text=True
    )
    w, h = result.stdout.strip().split(",")
    return int(w), int(h)


VR_MIN_PX = 5760  # 6k — below this, auto-render flat (no VR metadata)


def is_vr_resolution(width: int, height: int) -> bool:
    return max(width, height) >= VR_MIN_PX


def resolution_label(width: int, height: int) -> str:
    """Map frame size to a short suffix (8k / 6k / 4k / 1080p)."""
    px = max(width, height)
    if px >= 7680:
        return "8k"
    if px >= VR_MIN_PX:
        return "6k"
    if px >= 3840:
        return "4k"
    if px >= 1920:
        return "1080p"
    return f"{height}p"


def output_suffix(flat: bool, stereo: str, width: int, height: int, framerate: int) -> str:
    res = resolution_label(width, height)
    fps = f"{framerate}fps"
    if flat:
        return f"{res} {fps}"
    return f"360{stereo} {res} {fps}"


STANDARD_FPS = (30, 60)
FPS_TOLERANCE = 2.0


def probe_audio_duration(audio: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(audio)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def detect_framerate(frame_count: int, audio: Path) -> int:
    """Infer fps from frame count / audio duration; must be close to 30 or 60."""
    duration = probe_audio_duration(audio)
    if duration <= 0:
        die(f"Invalid audio duration: {duration}s")

    detected = frame_count / duration
    nearest = min(STANDARD_FPS, key=lambda f: abs(detected - f))
    delta = abs(detected - nearest)

    info(f"Audio   : {audio.name}  ({duration:.3f}s)")
    info(f"Detected: {detected:.2f} fps  (frames / audio)")

    if delta > FPS_TOLERANCE:
        raise RenderError(
            f"Detected framerate {detected:.2f} fps does not match 30 or 60 fps "
            f"(nearest: {nearest}, off by {delta:.2f}). "
            f"Check VAM render settings or pass -r explicitly."
        )

    info(f"Using   : {nearest} fps")
    return nearest


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
    args = [
        "exiftool",
        "-XMP-GSpherical:Spherical=true",
        "-XMP-GSpherical:Stitched=true",
        "-XMP-GSpherical:ProjectionType=equirectangular",
        f"-XMP-GSpherical:StereoMode={stereo_mode}",
        "-overwrite_original",
        str(mp4),
    ]

    last_err = ""
    for attempt in range(1, 6):
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode == 0:
            ok(f"equirectangular / {stereo_mode}")
            return
        last_err = (result.stderr or result.stdout or "").strip()
        if attempt < 5:
            warn(f"exiftool busy or file locked (attempt {attempt}/5), retrying...")
            time.sleep(2)

    raise RenderError(
        f"Metadata injection failed after 5 attempts.\n"
        f"  Video was encoded successfully: {mp4}\n"
        f"  exiftool: {last_err or 'unknown error'}"
    )


# ── Encoding ──────────────────────────────────────────────────────────────────

def build_common_args(
    pattern: Path,
    audio: Path | None,
    framerate: int,
    audio_offset: float,
) -> list[str]:
    vf_parts = ["format=yuv420p"]

    args = ["-y", "-framerate", str(framerate)]
    # Positive offset delays video so audio begins first in the output timeline.
    if audio and audio_offset != 0:
        args += ["-itsoffset", str(audio_offset)]
    args += ["-i", str(pattern)]
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
    audio_offset: float,
    crf: int,
    cq: int,
    output: Path,
) -> str:
    """Try hevc_nvenc, fall back to libx265. Returns encoder name used."""
    common = build_common_args(pattern, audio, framerate, audio_offset)

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
        raise RenderError("Encoding failed with both hevc_nvenc and libx265.")

    return "libx265"


def render_one(source: Path, args: argparse.Namespace) -> Path | None:
    """Render a single capture folder. Returns output path, or None if skipped."""
    if is_render_complete(source) and not args.force:
        warn(f"Skipping {source.name}: already rendered ({RENDER_COMPLETE_MARKER})")
        return None

    output_name = args.output_name or source.name
    output_dir = DEFAULT_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    info(f"Source  : {source}")

    if not is_export_complete(source):
        warn(f"Skipping {source.name}: export not complete (no WAV audio yet)")
        return None

    frames, ext = find_frames(source)
    pattern = frame_pattern(frames, ext)
    audio = find_audio(source)
    src_w, src_h = probe_resolution(frames[0])
    flat = args.flat or not is_vr_resolution(src_w, src_h)

    info(f"Frames  : {len(frames)} x .{ext}  ({src_w}x{src_h})")
    if flat and not args.flat:
        info("Mode    : flat (source below 6k — skipping VR metadata)")
    elif flat:
        info("Mode    : flat")
    else:
        info("Mode    : VR360")

    if args.framerate is not None:
        framerate = args.framerate
        info(f"Framerate: {framerate} fps (manual)")
        if audio:
            info(f"Audio   : {audio.name}")
    elif audio:
        framerate = detect_framerate(len(frames), audio)
    else:
        raise RenderError("No audio file found — cannot auto-detect framerate. Pass -r explicitly.")

    suffix = output_suffix(flat, args.stereo, src_w, src_h, framerate)
    output = output_dir / f"{output_name} {suffix}.mp4"
    info(f"Output  : {output}")

    if output.exists() and not args.force:
        warn(f"Skipping {source.name}: output already exists (use --force to overwrite)")
        return None

    if audio and args.audio_offset != 0:
        if args.audio_offset > 0:
            info(f"AV sync : audio leads video by {args.audio_offset:g}s")
        else:
            info(f"AV sync : video leads audio by {-args.audio_offset:g}s")

    t0 = time.perf_counter()
    encoder = encode(
        pattern, audio, len(frames), framerate,
        args.audio_offset if audio else 0.0, args.crf, args.cq, output,
    )
    elapsed = time.perf_counter() - t0

    enc_fps = len(frames) / elapsed if elapsed > 0 else 0
    size_mb = output.stat().st_size / 1_048_576
    ok(f"{encoder}  |  {enc_fps:.1f} encode-fps  |  {elapsed:.1f}s  |  {size_mb:.2f} MB")

    if flat:
        info("Metadata: skipped (flat)")
    else:
        if not shutil.which("exiftool"):
            raise RenderError("'exiftool' not found on PATH. Please install it first.")
        inject_metadata(output, args.stereo)

    return output


def run_batch(args: argparse.Namespace, pending: list[Path] | None = None) -> tuple[int, int, int]:
    """Render all pending folders. Returns (completed, skipped, failed)."""
    if pending is None:
        pending = find_pending_folders(force=args.force)
    if not pending:
        return 0, 0, 0

    info(f"Batch   : {len(pending)} folder(s) to render in {DEFAULT_VR_RENDERS}")
    completed = 0
    skipped = 0
    failed = 0

    for index, source in enumerate(pending, start=1):
        print(f"\n{'-' * 55}")
        step(f"Batch [{index}/{len(pending)}] {source.name}")
        print(f"NOTIFY:Render started|{source.name}", flush=True)
        try:
            output = render_one(source.resolve(), args)
        except RenderError as exc:
            failed += 1
            warn(f"Failed [{index}/{len(pending)}] {source.name}: {exc}")
            print(f"NOTIFY:Render failed|{source.name}: {exc}", flush=True)
            continue

        if output is None:
            skipped += 1
            continue

        write_render_complete(source, output)
        completed += 1
        ok(f"Done [{index}/{len(pending)}]: {output.name}")
        print(f"NOTIFY:Render complete|{output.name}", flush=True)

    if completed or skipped or failed:
        print(f"\n{'=' * 55}")
        print(f"  Batch complete: {completed} rendered, {skipped} skipped, {failed} failed")
        print("=" * 55)

    return completed, skipped, failed


SPINNER = "|/-\\"


def wait_with_spinner(seconds: float, label: str) -> None:
    """Wait with a single-line spinner (overwrites itself, no log spam)."""
    end = time.perf_counter() + seconds
    i = 0
    try:
        while True:
            remaining = end - time.perf_counter()
            if remaining <= 0:
                break
            ch = SPINNER[i % len(SPINNER)]
            print(f"\r  {ch} {label}   ", end="", flush=True)
            i += 1
            time.sleep(min(SPINNER_TICK_SEC, remaining))
    finally:
        print("\r" + " " * 50 + "\r", end="", flush=True)


def watch_and_render(args: argparse.Namespace) -> None:
    """Poll VR_Renders for pending folders and render them."""
    info(f"Watching {DEFAULT_VR_RENDERS} every {args.interval}s (Ctrl+C to stop)")
    try:
        while True:
            pending = find_pending_folders(force=args.force)
            if not pending:
                wait_with_spinner(args.interval, "Watching for new renders")
                continue

            run_batch(args, pending)
            wait_with_spinner(args.interval, "Watching for new renders")
    except KeyboardInterrupt:
        print("\nWatch stopped.\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Assemble VAM frame sequence into a 360 VR video."
    )
    parser.add_argument("source", nargs="?", default=None,
                        help="Folder with frame sequence and WAV "
                             f"(default: watch {DEFAULT_VR_RENDERS} for pending renders)")
    parser.add_argument("-r", "--framerate", type=int, default=None,
                        help="Output framerate (default: auto-detect from frames/audio)")
    parser.add_argument("--crf", type=int, default=20, help="libx265 quality (0–51, lower=better)")
    parser.add_argument("--cq",  type=int, default=20, help="hevc_nvenc quality (0–51, lower=better)")
    parser.add_argument("--stereo", default="mono",
                        choices=["mono", "left-right", "top-bottom"])
    parser.add_argument("--output-name", help="Base name for output file (default: folder name)")
    parser.add_argument(
        "--audio-offset", type=float, default=-0.3,
        help="Seconds audio leads video when muxing (negative = video leads; default: -0.3, use 0 to disable)",
    )
    parser.add_argument(
        "--flat", action="store_true",
        help="Force flat/non-VR output (skip 360 metadata even for 6k+ sources)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite output and re-render folders marked complete",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Render pending folders once and exit (default: watch every 10s)",
    )
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_WATCH_INTERVAL,
        help=f"Watch poll interval in seconds (default: {DEFAULT_WATCH_INTERVAL})",
    )
    args = parser.parse_args()

    if args.interval <= 0:
        die("--interval must be a positive number of seconds")

    # Dependency checks
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            die(f"'{tool}' not found on PATH. Please install it first.")

    print(f"\n" + "=" * 55)
    print(f"  VAM VR360 Renderer")
    print("=" * 55)

    if args.source is None:
        if args.once:
            completed, skipped, failed = run_batch(args)
            if completed == 0 and skipped == 0 and failed == 0:
                exit_notice(f"No pending render folders in {DEFAULT_VR_RENDERS}.")
            print()
            if failed:
                sys.exit(1)
            return
        watch_and_render(args)
        return

    source = Path(args.source).resolve()
    if not source.is_dir():
        die(f"Source folder not found: {source}")

    try:
        output = render_one(source, args)
    except RenderError as exc:
        die(str(exc))

    if output is None:
        exit_notice(f"Nothing rendered for: {source}")

    write_render_complete(source, output)

    print("\n" + "=" * 55)
    print(f"  Done: {output}")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    main()
