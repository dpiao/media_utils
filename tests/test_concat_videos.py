"""
Tests for concat_videos.py

Run with:  pytest test_concat_videos.py -v
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import concat_videos as sut


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _video_stream(**overrides) -> dict:
    base = {
        "codec_type": "video",
        "codec_name": "hevc",
        "width": 3840,
        "height": 1920,
        "pix_fmt": "yuv420p",
        "r_frame_rate": "60/1",
    }
    base.update(overrides)
    return base


def _audio_stream(**overrides) -> dict:
    base = {
        "codec_type": "audio",
        "codec_name": "aac",
        "sample_rate": "48000",
        "channels": 2,
        "channel_layout": "stereo",
    }
    base.update(overrides)
    return base


def _probe_payload(
    *,
    video: dict | None = None,
    audio: dict | None = None,
    duration: str = "10.0",
) -> dict:
    streams = [video or _video_stream()]
    if audio is not None:
        streams.append(audio)
    return {
        "streams": streams,
        "format": {"duration": duration},
    }


def _media(path: str, **kwargs) -> sut.MediaInfo:
    payload = _probe_payload(**kwargs)
    return sut.parse_probe_json(Path(path), payload)


# ── parse_probe_json ──────────────────────────────────────────────────────────

def test_parse_probe_json_with_audio() -> None:
    info = _media("clip.mp4", audio=_audio_stream())
    assert info.video.codec_name == "hevc"
    assert info.video.width == 3840
    assert info.audio is not None
    assert info.audio.sample_rate == 48000
    assert info.duration == 10.0


def test_parse_probe_json_without_audio() -> None:
    info = _media("clip.mp4")
    assert info.audio is None


def test_parse_probe_json_no_video_raises() -> None:
    with pytest.raises(ValueError, match="No video stream"):
        sut.parse_probe_json(Path("x.mp4"), {"streams": [_audio_stream()], "format": {}})


# ── parse_frame_rate ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("rate,expected", [
    ("60/1", 60.0),
    ("30000/1001", 30000 / 1001),
    ("30", 30.0),
])
def test_parse_frame_rate(rate: str, expected: float) -> None:
    assert sut.parse_frame_rate(rate) == pytest.approx(expected)


# ── validate_compatibility ────────────────────────────────────────────────────

def test_validate_compatibility_passes() -> None:
    media = [_media("a.mp4"), _media("b.mp4")]
    assert sut.validate_compatibility(media) == []


def test_validate_compatibility_height_mismatch() -> None:
    ref = _media("a.mp4")
    other = _media("b.mp4", video=_video_stream(height=1080))
    problems = sut.validate_compatibility([ref, other])
    assert len(problems) == 1
    assert problems[0].path.name == "b.mp4"
    assert any(f[0] == "video.height" for f in problems[0].fields)


def test_validate_compatibility_codec_mismatch() -> None:
    ref = _media("a.mp4")
    other = _media("b.mp4", video=_video_stream(codec_name="h264"))
    problems = sut.validate_compatibility([ref, other])
    assert any(f[0] == "video.codec_name" for f in problems[0].fields)


def test_validate_compatibility_fps_mismatch() -> None:
    ref = _media("a.mp4")
    other = _media("b.mp4", video=_video_stream(r_frame_rate="30/1"))
    problems = sut.validate_compatibility([ref, other])
    assert any(f[0] == "video.r_frame_rate" for f in problems[0].fields)


def test_validate_compatibility_audio_presence_mismatch() -> None:
    ref = _media("a.mp4", audio=_audio_stream())
    other = _media("b.mp4")
    problems = sut.validate_compatibility([ref, other])
    assert any(f[0] == "audio.present" for f in problems[0].fields)


def test_validate_compatibility_audio_sample_rate_mismatch() -> None:
    ref = _media("a.mp4", audio=_audio_stream())
    other = _media("b.mp4", audio=_audio_stream(sample_rate="44100"))
    problems = sut.validate_compatibility([ref, other])
    assert any(f[0] == "audio.sample_rate" for f in problems[0].fields)


# ── build_concat_list ─────────────────────────────────────────────────────────

def test_build_concat_list_paths() -> None:
    paths = [Path(r"C:\Movies\a.mp4"), Path(r"C:\Movies\b.mp4")]
    text = sut.build_concat_list(paths)
    assert "file 'C:/Movies/a.mp4'" in text
    assert "file 'C:/Movies/b.mp4'" in text


def test_build_concat_list_escapes_quotes(tmp_path: Path) -> None:
    quoted = tmp_path / "clip's.mp4"
    quoted.touch()
    text = sut.build_concat_list([quoted])
    assert "clip'\\''s.mp4" in text


# ── VR metadata ───────────────────────────────────────────────────────────────

def test_detect_vr_stereo_mode_from_filename() -> None:
    assert sut.detect_vr_stereo_mode(Path("20260615-220740 360mono 6k 30fps.mp4")) == "mono"
    assert sut.detect_vr_stereo_mode(Path("clip 360left-right 6k 30fps.mp4")) == "left-right"
    assert sut.detect_vr_stereo_mode(Path("flat 4k 30fps.mp4")) is None


def test_detect_vr_stereo_mode_from_exiftool() -> None:
    payload = json.dumps([{"Spherical": "True", "StereoMode": "mono"}])
    with patch("shutil.which", return_value="/usr/bin/exiftool"):
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=payload, stderr="")):
            assert sut.detect_vr_stereo_mode(Path("anything.mp4")) == "mono"


def test_resolve_vr_stereo_mode_mixed_inputs() -> None:
    vr = Path("a 360mono 6k 30fps.mp4")
    flat = Path("flat.mp4")
    with patch.object(sut, "detect_vr_stereo_mode", side_effect=["mono", None]):
        with pytest.raises(SystemExit):
            sut.resolve_vr_stereo_mode([vr, flat])


def test_resolve_vr_stereo_mode_different_stereo() -> None:
    a = Path("a 360mono 6k 30fps.mp4")
    b = Path("b 360left-right 6k 30fps.mp4")
    with patch.object(sut, "detect_vr_stereo_mode", side_effect=["mono", "left-right"]):
        with pytest.raises(SystemExit):
            sut.resolve_vr_stereo_mode([a, b])


def test_inject_vr_metadata_replaces_unicode_output(tmp_path: Path) -> None:
    src = tmp_path / "out \u9a91\u5177.mp4"
    src.write_bytes(b"video")
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
        with patch("os.link") as mock_link:
            with patch("os.replace") as mock_replace:
                sut.inject_vr_metadata(src, "mono")
    mock_link.assert_called_once()
    mock_replace.assert_called_once()
    replaced_to = mock_replace.call_args.args[1]
    assert replaced_to == src.resolve()


def test_inject_vr_metadata_skips_replace_for_ascii_path(tmp_path: Path) -> None:
    src = tmp_path / "out.mp4"
    src.write_bytes(b"video")
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
        with patch("os.replace") as mock_replace:
            sut.inject_vr_metadata(src, "mono")
    mock_replace.assert_not_called()


def test_concat_videos_injects_vr_metadata(tmp_path: Path) -> None:
    a = tmp_path / "a 360mono 6k 30fps.mp4"
    b = tmp_path / "b 360mono 6k 30fps.mp4"
    out = tmp_path / "out.mp4"
    a.write_bytes(b"x")
    b.write_bytes(b"x")

    payload = json.dumps(_probe_payload(audio=_audio_stream()))

    def fake_run(cmd, **kwargs):
        if cmd[0] == "ffprobe":
            return MagicMock(returncode=0, stdout=payload, stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        with patch.object(sut, "run_concat") as mock_concat:
            with patch.object(sut, "inject_vr_metadata") as mock_inject:
                out.write_bytes(b"out")
                mock_concat.side_effect = lambda *_a, **_k: out.write_bytes(b"out")
                sut.concat_videos([a, b], out, force=True)

    mock_inject.assert_called_once_with(out.resolve(), "mono")


def test_concat_videos_skips_vr_metadata_for_flat_inputs(tmp_path: Path) -> None:
    a = tmp_path / "a flat 4k 30fps.mp4"
    b = tmp_path / "b flat 4k 30fps.mp4"
    out = tmp_path / "out.mp4"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    payload = json.dumps(_probe_payload())

    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=payload, stderr="")):
        with patch.object(sut, "run_concat"):
            with patch.object(sut, "inject_vr_metadata") as mock_inject:
                out.write_bytes(b"out")
                sut.concat_videos([a, b], out, force=True)

    mock_inject.assert_not_called()


# ── run_concat ────────────────────────────────────────────────────────────────

def test_run_concat_calls_ffmpeg_with_list_file(tmp_path: Path) -> None:
    list_path = tmp_path / "concat.txt"

    def fake_mkstemp(**kwargs):
        fd = os.open(list_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
        return fd, str(list_path)

    with patch("concat_videos.tempfile.mkstemp", side_effect=fake_mkstemp):
        with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            sut.run_concat([Path("a.mp4"), Path("b.mp4")], Path("out.mp4"), force=False)

    cmd = mock_run.call_args.args[0]
    assert cmd[0] == "ffmpeg"
    assert str(list_path) in cmd
    assert "pipe:0" not in cmd
    assert "-n" in cmd


def test_run_concat_force_uses_y_flag() -> None:
    with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        sut.run_concat([Path("a.mp4")], Path("out.mp4"), force=True)
    assert "-y" in mock_run.call_args.args[0]


# ── concat_videos dry-run ─────────────────────────────────────────────────────

def test_concat_videos_dry_run_skips_ffmpeg(tmp_path: Path) -> None:
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"x")
    b.write_bytes(b"x")

    payload = json.dumps(_probe_payload(audio=_audio_stream()))

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
        result = sut.concat_videos([a, b], tmp_path / "out.mp4", dry_run=True)

    assert result is None
    ffprobe_calls = [c for c in mock_run.call_args_list if c.args[0][0] == "ffprobe"]
    assert len(ffprobe_calls) == 2


# ── main / CLI guards ─────────────────────────────────────────────────────────

def test_main_dry_run_ok(capsys, tmp_path: Path) -> None:
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    payload = json.dumps(_probe_payload())

    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=payload, stderr="")):
        with patch("shutil.which", return_value="/usr/bin/tool"):
            sut.main(["--dry-run", str(a), str(b)])

    out = capsys.readouterr().out
    assert "Dry run OK" in out


def test_main_fewer_than_two_inputs_exits() -> None:
    with patch("shutil.which", return_value="/usr/bin/tool"):
        with pytest.raises(SystemExit):
            sut.main(["out.mp4", "only.mp4"])


def test_main_missing_input_exits(tmp_path: Path) -> None:
    a = tmp_path / "a.mp4"
    a.write_bytes(b"x")
    missing = tmp_path / "missing.mp4"

    with patch("shutil.which", return_value="/usr/bin/tool"):
        with pytest.raises(SystemExit):
            sut.main(["-o", str(tmp_path / "out.mp4"), str(a), str(missing)])


def test_main_output_exists_without_force_exits(tmp_path: Path) -> None:
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    out = tmp_path / "out.mp4"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    out.write_bytes(b"existing")

    with patch("shutil.which", return_value="/usr/bin/tool"):
        with pytest.raises(SystemExit):
            sut.main(["-o", str(out), str(a), str(b)])


def test_main_mismatch_reports_error(capsys, tmp_path: Path) -> None:
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"x")
    b.write_bytes(b"x")

    good = json.dumps(_probe_payload())
    bad = json.dumps(_probe_payload(video=_video_stream(height=1080)))

    def fake_run(cmd, **kwargs):
        path = cmd[-1]
        payload = bad if "b.mp4" in path else good
        return MagicMock(returncode=0, stdout=payload, stderr="")

    with patch("shutil.which", return_value="/usr/bin/tool"):
        with patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(SystemExit):
                sut.main(["--dry-run", str(a), str(b)])

    err = capsys.readouterr().err
    assert "not concatenatable" in err
    assert "video.height" in err
