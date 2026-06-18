#!/usr/bin/env python3
"""
concat_videos.py — Concatenate N compatible videos into one file (stream copy).

Probes each input with ffprobe, validates dimensions/codecs/fps/audio match,
then concatenates via ffmpeg concat demuxer with -c copy (lossless).
Re-injects 360 spherical metadata on the output when inputs are VR renders.

Dependencies: ffmpeg, ffprobe, exiftool (exiftool only needed for VR outputs)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


FPS_TOLERANCE = 0.001
VR_FILENAME_RE = re.compile(r"360(mono|left-right|top-bottom)\b", re.IGNORECASE)
STEREO_MODES = frozenset({"mono", "left-right", "top-bottom"})
_SUBPROCESS_TEXT = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"}


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


# ── Stream metadata ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class VideoStream:
    codec_name: str
    width: int
    height: int
    pix_fmt: str
    r_frame_rate: str


@dataclass(frozen=True)
class AudioStream:
    codec_name: str
    sample_rate: int
    channels: int
    channel_layout: str


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration: float
    video: VideoStream
    audio: AudioStream | None


@dataclass(frozen=True)
class CompatibilityDiff:
    path: Path
    fields: tuple[tuple[str, str, str], ...]  # (field, actual, expected)


def parse_frame_rate(rate: str) -> float:
    """Parse ffprobe r_frame_rate like '30000/1001' or '30/1'."""
    if "/" in rate:
        num, den = rate.split("/", 1)
        denominator = float(den)
        if denominator == 0:
            return 0.0
        return float(num) / denominator
    return float(rate)


def _stream_field(stream: dict, key: str, default: str = "") -> str:
    value = stream.get(key, default)
    return str(value) if value is not None else default


def parse_probe_json(path: Path, data: dict) -> MediaInfo:
    streams = data.get("streams") or []
    fmt = data.get("format") or {}

    video_raw = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video_raw is None:
        raise ValueError(f"No video stream in {path.name}")

    video = VideoStream(
        codec_name=_stream_field(video_raw, "codec_name"),
        width=int(video_raw["width"]),
        height=int(video_raw["height"]),
        pix_fmt=_stream_field(video_raw, "pix_fmt"),
        r_frame_rate=_stream_field(video_raw, "r_frame_rate"),
    )

    audio_raw = next((s for s in streams if s.get("codec_type") == "audio"), None)
    audio: AudioStream | None = None
    if audio_raw is not None:
        layout = _stream_field(audio_raw, "channel_layout", "unknown")
        audio = AudioStream(
            codec_name=_stream_field(audio_raw, "codec_name"),
            sample_rate=int(audio_raw.get("sample_rate") or 0),
            channels=int(audio_raw.get("channels") or 0),
            channel_layout=layout if layout else "unknown",
        )

    duration = float(fmt.get("duration") or 0.0)
    return MediaInfo(path=path, duration=duration, video=video, audio=audio)


def probe_media(path: Path) -> MediaInfo:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_streams", "-show_format",
            "-of", "json",
            str(path),
        ],
        check=False,
        **_SUBPROCESS_TEXT,
    )
    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip() or "ffprobe failed"
        raise ValueError(f"{path.name}: {err}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name}: invalid ffprobe JSON ({exc})") from exc

    return parse_probe_json(path, data)


def _compare_video(ref: VideoStream, other: VideoStream) -> list[tuple[str, str, str]]:
    diffs: list[tuple[str, str, str]] = []
    if ref.width != other.width:
        diffs.append(("video.width", str(other.width), str(ref.width)))
    if ref.height != other.height:
        diffs.append(("video.height", str(other.height), str(ref.height)))
    if ref.codec_name != other.codec_name:
        diffs.append(("video.codec_name", other.codec_name, ref.codec_name))
    if ref.pix_fmt != other.pix_fmt:
        diffs.append(("video.pix_fmt", other.pix_fmt, ref.pix_fmt))
    ref_fps = parse_frame_rate(ref.r_frame_rate)
    other_fps = parse_frame_rate(other.r_frame_rate)
    if abs(ref_fps - other_fps) > FPS_TOLERANCE:
        diffs.append(("video.r_frame_rate", other.r_frame_rate, ref.r_frame_rate))
    return diffs


def _compare_audio(ref: AudioStream, other: AudioStream) -> list[tuple[str, str, str]]:
    diffs: list[tuple[str, str, str]] = []
    if ref.codec_name != other.codec_name:
        diffs.append(("audio.codec_name", other.codec_name, ref.codec_name))
    if ref.sample_rate != other.sample_rate:
        diffs.append(("audio.sample_rate", str(other.sample_rate), str(ref.sample_rate)))
    if ref.channels != other.channels:
        diffs.append(("audio.channels", str(other.channels), str(ref.channels)))
    if ref.channel_layout != other.channel_layout:
        diffs.append(("audio.channel_layout", other.channel_layout, ref.channel_layout))
    return diffs


def validate_compatibility(media: list[MediaInfo]) -> list[CompatibilityDiff]:
    if len(media) < 2:
        return []

    ref = media[0]
    ref_has_audio = ref.audio is not None
    problems: list[CompatibilityDiff] = []

    for item in media[1:]:
        fields: list[tuple[str, str, str]] = []
        fields.extend(_compare_video(ref.video, item.video))

        item_has_audio = item.audio is not None
        if ref_has_audio != item_has_audio:
            fields.append((
                "audio.present",
                "yes" if item_has_audio else "no",
                "yes" if ref_has_audio else "no",
            ))
        elif ref_has_audio and item.audio is not None and ref.audio is not None:
            fields.extend(_compare_audio(ref.audio, item.audio))

        if fields:
            problems.append(CompatibilityDiff(path=item.path, fields=tuple(fields)))

    return problems


def format_compatibility_report(
    media: list[MediaInfo],
    problems: list[CompatibilityDiff],
) -> str:
    ref = media[0]
    lines = [f"Inputs are not concatenatable (reference: {ref.path.name})", ""]
    for problem in problems:
        lines.append(f"  {problem.path.name}")
        for field, actual, expected in problem.fields:
            lines.append(f"    {field}: {actual} vs {expected}")
        lines.append("")
    return "\n".join(lines).rstrip()


def escape_concat_path(path: Path) -> str:
    """Escape path for ffmpeg concat demuxer (single-quoted file lines)."""
    posix = path.resolve().as_posix()
    return posix.replace("'", r"'\''")


def build_concat_list(paths: list[Path]) -> str:
    lines = [f"file '{escape_concat_path(p)}'" for p in paths]
    return "\n".join(lines) + "\n"


def run_concat(paths: list[Path], output: Path, *, force: bool) -> None:
    concat_list = build_concat_list(paths)
    list_fd, list_path = tempfile.mkstemp(suffix=".txt", prefix="concat_")
    try:
        with open(list_fd, "w", encoding="utf-8") as f:
            f.write(concat_list)

        cmd = [
            "ffmpeg",
            "-f", "concat",
            "-safe", "0",
            "-i", list_path,
            "-c", "copy",
            str(output),
        ]
        if force:
            cmd.insert(1, "-y")
        else:
            cmd.insert(1, "-n")

        result = subprocess.run(
            cmd,
            check=False,
            **_SUBPROCESS_TEXT,
        )
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip() or "ffmpeg failed"
            die(err)
    finally:
        Path(list_path).unlink(missing_ok=True)


def _parse_spherical(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "yes", "1"}


def detect_vr_stereo_mode(path: Path) -> str | None:
    """Return stereo mode when path is a 360 VR video, else None."""
    if shutil.which("exiftool"):
        result = subprocess.run(
            [
                "exiftool", "-json",
                "-XMP-GSpherical:Spherical",
                "-XMP-GSpherical:StereoMode",
                str(path),
            ],
            check=False,
            **_SUBPROCESS_TEXT,
        )
        if result.returncode == 0 and result.stdout.strip():
            try:
                payload = json.loads(result.stdout)
                if isinstance(payload, list) and payload:
                    tags = payload[0]
                elif isinstance(payload, dict):
                    tags = payload
                else:
                    tags = {}
            except json.JSONDecodeError:
                tags = {}
            if _parse_spherical(tags.get("Spherical")):
                mode = str(tags.get("StereoMode") or "mono").strip().lower()
                return mode if mode in STEREO_MODES else "mono"

    match = VR_FILENAME_RE.search(path.name)
    if match:
        return match.group(1).lower()

    return None


def resolve_vr_stereo_mode(paths: list[Path]) -> str | None:
    """Detect VR mode from inputs; fail if VR settings differ or VR/flat are mixed."""
    modes = [detect_vr_stereo_mode(p) for p in paths]
    if len(set(modes)) > 1:
        lines = ["VR settings differ across inputs (reference: {})".format(paths[0].name), ""]
        ref = modes[0]
        for path, mode in zip(paths[1:], modes[1:]):
            if mode != ref:
                lines.append(f"  {path.name}: {mode or 'flat'} vs {ref or 'flat'}")
        die("\n".join(lines))
    return modes[0]


def inject_vr_metadata(mp4: Path, stereo_mode: str) -> None:
    step("Injecting 360 spherical metadata")
    if not shutil.which("exiftool"):
        die("'exiftool' not found on PATH. Required to restore VR metadata on output.")

    target = mp4.resolve()
    link_path: Path | None = None
    if str(target).isascii():
        exiftool_file = target
    else:
        link_path = target.parent / f".concat_vr_inject_{os.getpid()}.mp4"
        os.link(target, link_path)
        exiftool_file = link_path

    args = [
        "exiftool",
        "-XMP-GSpherical:Spherical=true",
        "-XMP-GSpherical:Stitched=true",
        "-XMP-GSpherical:ProjectionType=equirectangular",
        f"-XMP-GSpherical:StereoMode={stereo_mode}",
        "-overwrite_original",
        str(exiftool_file),
    ]

    injected_path = exiftool_file
    try:
        last_err = ""
        for attempt in range(1, 6):
            result = subprocess.run(args, **_SUBPROCESS_TEXT)
            if result.returncode == 0:
                break
            last_err = (result.stderr or result.stdout or "").strip()
            if attempt < 5:
                warn(f"exiftool failed (attempt {attempt}/5), retrying...")
                time.sleep(2)
        else:
            die(
                "Metadata injection failed after 5 attempts.\n"
                f"  Video was concatenated successfully: {mp4}\n"
                f"  exiftool: {last_err or 'unknown error'}"
            )

        if link_path is not None:
            os.replace(injected_path, target)
            link_path = None

        ok(f"equirectangular / {stereo_mode}")
    finally:
        if link_path is not None:
            link_path.unlink(missing_ok=True)
        backup = injected_path.with_name(injected_path.name + "_original")
        if backup.is_file():
            backup.unlink()


def resolve_inputs(paths: list[Path]) -> list[Path]:
    resolved: list[Path] = []
    for path in paths:
        p = path.resolve()
        if not p.is_file():
            die(f"Input not found: {path}")
        resolved.append(p)
    return resolved


def probe_all(paths: list[Path]) -> list[MediaInfo]:
    media: list[MediaInfo] = []
    for path in paths:
        try:
            media.append(probe_media(path))
        except ValueError as exc:
            die(str(exc))
    return media


def print_media_summary(media: list[MediaInfo]) -> None:
    ref = media[0]
    v = ref.video
    info(f"Reference: {ref.path.name}")
    info(f"  Video : {v.codec_name} {v.width}x{v.height} {v.pix_fmt} @ {v.r_frame_rate}")
    if ref.audio:
        a = ref.audio
        info(f"  Audio : {a.codec_name} {a.sample_rate} Hz {a.channels}ch {a.channel_layout}")
    else:
        info("  Audio : none")
    total_dur = sum(m.duration for m in media)
    info(f"  Clips : {len(media)}  (~{total_dur:.1f}s total)")


def concat_videos(
    inputs: list[Path],
    output: Path,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> Path | None:
    if len(inputs) < 2:
        die("Need at least 2 input videos")

    resolved_inputs = resolve_inputs(inputs)
    output = output.resolve()

    if output.exists() and not force and not dry_run:
        die(f"Output already exists: {output}  (use --force to overwrite)")

    step("Probing inputs")
    media = probe_all(resolved_inputs)
    print_media_summary(media)

    step("Checking compatibility")
    problems = validate_compatibility(media)
    if problems:
        die(format_compatibility_report(media, problems))

    ok(f"{len(media)} files compatible")

    vr_mode = resolve_vr_stereo_mode(resolved_inputs)
    if vr_mode:
        info(f"VR      : equirectangular / {vr_mode}")

    if dry_run:
        return None

    step(f"Concatenating -> {output.name}")
    run_concat(resolved_inputs, output, force=force)
    if vr_mode:
        inject_vr_metadata(output, vr_mode)
    size = output.stat().st_size
    ok(f"Wrote {output}  ({size:,} bytes)")
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Concatenate compatible videos (ffmpeg stream copy).",
    )
    parser.add_argument(
        "-o", "--output",
        help="Output video path (default: first positional arg)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs only; do not write output",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing output file",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Output (unless -o) and 2+ input videos, or inputs only with --dry-run",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.dry_run:
        if args.output is not None:
            die("--dry-run does not take an output path; pass input videos only")
        if len(args.paths) < 2:
            die("Need at least 2 input videos")
        args.output_path = None
        args.input_paths = args.paths
        return args

    if args.output is not None:
        if len(args.paths) < 2:
            die("Need at least 2 input videos when using -o/--output")
        args.output_path = Path(args.output)
        args.input_paths = args.paths
    else:
        if len(args.paths) < 3:
            die("Need output plus at least 2 inputs, or use -o/--output")
        args.output_path = args.paths[0]
        args.input_paths = args.paths[1:]

    return args


def main(argv: list[str] | None = None) -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            die(f"'{tool}' not found on PATH. Please install it first.")

    args = parse_args(argv)

    print("\n" + "=" * 55)
    print("  Video Concatenator")
    print("=" * 55)

    result = concat_videos(
        args.input_paths,
        args.output_path or Path("_unused_"),
        dry_run=args.dry_run,
        force=args.force,
    )

    if args.dry_run:
        print("\n" + "=" * 55)
        print("  Dry run OK")
        print("=" * 55 + "\n")
        return

    print("\n" + "=" * 55)
    print(f"  Done: {result}")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    main()
