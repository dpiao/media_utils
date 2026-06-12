"""
Tests for sync_media_to_s3.py

Run with:  pytest test_sync_media_to_s3.py -v
"""

import time
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sync_media_to_s3 as sut


# ── is_temp ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("movie.part",        True),
    ("movie.crdownload",  True),
    ("movie.!qb",         True),
    ("movie.tmp",         True),
    ("movie.download",    True),
    ("movie.PART",        True),   # case-insensitive
    ("movie.mkv",         False),
    ("movie.mp4",         False),
    ("movie.avi",         False),
    ("no_extension",      False),
])
def test_is_temp(name: str, expected: bool) -> None:
    assert sut.is_temp(Path(name)) == expected


# ── s3_key ────────────────────────────────────────────────────────────────────

def test_s3_key_simple() -> None:
    root = Path(r"C:\Movies")
    file = Path(r"C:\Movies\Action\foo.mkv")
    key = sut.s3_key(root, file, "s3://park.movies.archive/")
    assert key == "s3://park.movies.archive/Action/foo.mkv"


def test_s3_key_strips_trailing_slash() -> None:
    root = Path(r"C:\Movies")
    file = Path(r"C:\Movies\foo.mp4")
    key = sut.s3_key(root, file, "s3://park.movies.archive/")
    assert key == "s3://park.movies.archive/foo.mp4"


def test_s3_key_nested() -> None:
    root = Path(r"E:\Movies")
    file = Path(r"E:\Movies\Drama\2024\bar.mkv")
    key = sut.s3_key(root, file, "s3://park.movies.archive/")
    assert key == "s3://park.movies.archive/Drama/2024/bar.mkv"


# ── run_aws dry-run ───────────────────────────────────────────────────────────

def test_run_aws_dry_run_does_not_call_subprocess(capsys) -> None:
    with patch("subprocess.run") as mock_run:
        sut.run_aws(["aws", "s3", "cp", "foo", "s3://x/foo"], dry_run=True)
        mock_run.assert_not_called()
    out = capsys.readouterr().out
    assert "dry-run" in out


def test_run_aws_calls_subprocess() -> None:
    mock_result = MagicMock(returncode=0)
    with patch("subprocess.run", return_value=mock_result) as mock_run:
        sut.run_aws(["aws", "s3", "cp", "a", "b"], dry_run=False)
        mock_run.assert_called_once_with(["aws", "s3", "cp", "a", "b"])


def test_run_aws_logs_upload_path(capsys) -> None:
    mock_result = MagicMock(returncode=0)
    with patch("subprocess.run", return_value=mock_result):
        sut.run_aws(["aws", "s3", "cp", r"C:\Movies\foo.mkv", "s3://bucket/foo.mkv"], dry_run=False)
    out = capsys.readouterr().out
    assert r"C:\Movies\foo.mkv" in out
    assert "s3://bucket/foo.mkv" in out


def test_run_aws_logs_sync_path(capsys) -> None:
    mock_result = MagicMock(returncode=0)
    with patch("subprocess.run", return_value=mock_result):
        sut.run_aws(["aws", "s3", "sync", r"C:\Movies", "s3://bucket/"], dry_run=False)
    out = capsys.readouterr().out
    assert r"C:\Movies" in out
    assert "s3://bucket/" in out


def test_run_aws_warns_on_nonzero(capsys) -> None:
    mock_result = MagicMock(returncode=1)
    with patch("subprocess.run", return_value=mock_result):
        sut.run_aws(["aws", "s3", "cp", "a", "b"], dry_run=False)
    err = capsys.readouterr().err
    assert "1" in err


# ── upload_file ───────────────────────────────────────────────────────────────

def test_upload_file_builds_correct_command(tmp_path) -> None:
    f = tmp_path / "sub" / "movie.mkv"
    f.parent.mkdir()
    f.touch()

    with patch("sync_media_to_s3.run_aws") as mock_run:
        sut.upload_file(tmp_path, f, "s3://park.movies.archive/", dry_run=False, ignore=sut.IgnoreRules.empty())
        expected_dest = f"s3://park.movies.archive/sub/movie.mkv"
        mock_run.assert_called_once_with(
            ["aws", "s3", "cp", str(f), expected_dest], False
        )


def test_upload_file_skips_ignored(tmp_path) -> None:
    f = tmp_path / "V Vam My Render" / "20260610-024155_foo.mkv"
    f.parent.mkdir(parents=True)
    f.touch()
    ignore = sut.IgnoreRules(["V Vam My Render/202?????-??????*"], [])

    with patch("sync_media_to_s3.run_aws") as mock_run:
        sut.upload_file(tmp_path, f, "s3://park.movies.archive/", dry_run=False, ignore=ignore)
        mock_run.assert_not_called()


# ── MediaHandler ──────────────────────────────────────────────────────────────

def _make_handler(tmp_path: Path, ignore: sut.IgnoreRules | None = None) -> tuple[sut.MediaHandler, MagicMock]:
    checker = MagicMock()
    handler = sut.MediaHandler(
        tmp_path, "s3://park.movies.archive/", checker, ignore or sut.IgnoreRules.empty()
    )
    return handler, checker


def _event(src: str, is_directory: bool = False) -> MagicMock:
    e = MagicMock()
    e.src_path = src
    e.is_directory = is_directory
    return e


def _moved_event(dest: str, is_directory: bool = False) -> MagicMock:
    e = MagicMock()
    e.dest_path = dest
    e.is_directory = is_directory
    return e


def test_handler_on_created_enqueues(tmp_path) -> None:
    f = tmp_path / "movie.mkv"
    f.touch()
    handler, checker = _make_handler(tmp_path)
    handler.on_created(_event(str(f)))
    checker.enqueue.assert_called_once_with(f, tmp_path, "s3://park.movies.archive/")


def test_handler_on_modified_enqueues(tmp_path) -> None:
    f = tmp_path / "movie.mp4"
    f.touch()
    handler, checker = _make_handler(tmp_path)
    handler.on_modified(_event(str(f)))
    checker.enqueue.assert_called_once()


def test_handler_on_moved_enqueues(tmp_path) -> None:
    f = tmp_path / "movie.avi"
    f.touch()
    handler, checker = _make_handler(tmp_path)
    handler.on_moved(_moved_event(str(f)))
    checker.enqueue.assert_called_once()


def test_handler_skips_temp_extension(tmp_path) -> None:
    f = tmp_path / "movie.part"
    f.touch()
    handler, checker = _make_handler(tmp_path)
    handler.on_created(_event(str(f)))
    checker.enqueue.assert_not_called()


def test_handler_skips_ignored_files(tmp_path) -> None:
    f = tmp_path / "V Vam My Render" / "20260610-024155_foo.mkv"
    f.parent.mkdir(parents=True)
    f.touch()
    ignore = sut.IgnoreRules(["V Vam My Render/202?????-??????*"], [])
    handler, checker = _make_handler(tmp_path, ignore)
    handler.on_created(_event(str(f)))
    checker.enqueue.assert_not_called()


def test_handler_logs_ignored_once(capsys, tmp_path) -> None:
    f = tmp_path / "V Vam My Render" / "20260610-024155_foo.mkv"
    f.parent.mkdir(parents=True)
    f.touch()
    ignore = sut.IgnoreRules(["V Vam My Render/202?????-??????*"], [])
    handler, _ = _make_handler(tmp_path, ignore)
    handler.on_created(_event(str(f)))
    handler.on_modified(_event(str(f)))
    out = capsys.readouterr().out
    assert sum(1 for line in out.splitlines() if "ignored  " in line) == 1
    assert str(f) in out


def test_handler_skips_directory_events(tmp_path) -> None:
    handler, checker = _make_handler(tmp_path)
    handler.on_created(_event(str(tmp_path / "subdir"), is_directory=True))
    checker.enqueue.assert_not_called()


def test_handler_skips_nonexistent_file(tmp_path) -> None:
    handler, checker = _make_handler(tmp_path)
    handler.on_created(_event(str(tmp_path / "ghost.mkv")))
    checker.enqueue.assert_not_called()


# ── StabilityChecker ──────────────────────────────────────────────────────────

def test_stability_checker_uploads_after_stable(tmp_path) -> None:
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"x" * 100)

    uploaded: list[str] = []

    with patch("sync_media_to_s3.upload_file", side_effect=lambda root, p, *a, **k: uploaded.append(str(p))):
        checker = sut.StabilityChecker(stable_secs=1, dry_run=False, ignore=sut.IgnoreRules.empty())
        checker.enqueue(f, tmp_path, "s3://park.movies.archive/")
        time.sleep(3)

    assert str(f) in uploaded


def test_stability_checker_duplicate_enqueue_logs_once(capsys, tmp_path) -> None:
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"x" * 100)
    checker = sut.StabilityChecker(stable_secs=60, dry_run=False, ignore=sut.IgnoreRules.empty())
    checker.enqueue(f, tmp_path, "s3://park.movies.archive/")
    checker.enqueue(f, tmp_path, "s3://park.movies.archive/")
    out = capsys.readouterr().out
    assert out.count("queued") == 1


def test_stability_checker_does_not_upload_while_growing(tmp_path) -> None:
    f = tmp_path / "growing.mkv"
    f.write_bytes(b"x" * 10)

    uploaded: list[str] = []
    stop = threading.Event()

    def grow():
        for _ in range(6):
            if stop.is_set():
                break
            time.sleep(0.4)
            with open(f, "ab") as fh:
                fh.write(b"x" * 10)

    with patch("sync_media_to_s3.upload_file", side_effect=lambda p, *a, **k: uploaded.append(str(p))):
        checker = sut.StabilityChecker(stable_secs=2, dry_run=False, ignore=sut.IgnoreRules.empty())
        checker.enqueue(f, tmp_path, "s3://park.movies.archive/")
        t = threading.Thread(target=grow, daemon=True)
        t.start()
        time.sleep(1.5)
        stop.set()
        t.join()

    assert str(f) not in uploaded


def test_stability_checker_dry_run_passes_flag(tmp_path) -> None:
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"data")

    calls: list[tuple] = []

    def capture(local_root, file_path, s3_prefix, dry_run, ignore):
        calls.append((file_path, dry_run))

    with patch("sync_media_to_s3.upload_file", side_effect=capture):
        checker = sut.StabilityChecker(stable_secs=1, dry_run=True, ignore=sut.IgnoreRules.empty())
        checker.enqueue(f, tmp_path, "s3://park.movies.archive/")
        time.sleep(3)

    assert calls, "upload_file was never called"
    _, dry_run_flag = calls[0]
    assert dry_run_flag is True


# ── initial_sync exclude flags ────────────────────────────────────────────────

def test_initial_sync_excludes_temp_extensions(tmp_path) -> None:
    with patch("sync_media_to_s3.run_aws") as mock_run:
        sut.initial_sync(tmp_path, "s3://park.movies.archive/", dry_run=True, ignore=sut.IgnoreRules.empty())
        args = mock_run.call_args[0][0]

    exclude_pairs = [
        args[i + 1] for i, a in enumerate(args) if a == "--exclude"
    ]
    for ext in sut.TEMP_EXTENSIONS:
        assert any(ext in pat for pat in exclude_pairs), f"{ext} not excluded"


# ── IgnoreRules ───────────────────────────────────────────────────────────────

def test_ignore_vam_timestamp_glob() -> None:
    ignore = sut.IgnoreRules(["V Vam My Render/202?????-??????*"], [])
    assert ignore.is_ignored("V Vam My Render/20260610-024155_foo.mkv")
    assert not ignore.is_ignored("V Vam My Render/My Finished Movie.mkv")


def test_ignore_regex() -> None:
    ignore = sut.IgnoreRules([], [__import__("re").compile(r"V Vam My Render/\d{8}-\d{6}")])
    assert ignore.is_ignored("V Vam My Render/20260610-024155")
    assert not ignore.is_ignored("V Vam My Render/other.mkv")


def test_ignore_glob_anywhere() -> None:
    ignore = sut.IgnoreRules(["**/Thumbs.db"], [])
    assert ignore.is_ignored("some/nested/Thumbs.db")


def test_ignore_load_from_file(tmp_path) -> None:
    ignore_file = tmp_path / "sync_media_to_s3.ignore"
    ignore_file.write_text("# comment\nfoo/*.part\nre: bar/\\d+\n", encoding="utf-8")
    ignore = sut.IgnoreRules.load(ignore_file)
    assert ignore.is_ignored("foo/x.part")
    assert ignore.is_ignored("bar/123")
    assert not ignore.is_ignored("foo/x.mkv")


def test_initial_sync_excludes_ignore_globs(tmp_path) -> None:
    ignore = sut.IgnoreRules(["V Vam My Render/202?????-??????*"], [])
    with patch("sync_media_to_s3.run_aws") as mock_run:
        sut.initial_sync(tmp_path, "s3://park.movies.archive/", dry_run=True, ignore=ignore)
        args = mock_run.call_args[0][0]
    assert "V Vam My Render/202?????-??????*" in args


# ── should_handle_modified ────────────────────────────────────────────────────

def test_modified_skips_unchanged_fingerprint(tmp_path) -> None:
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"same")
    fps: dict[Path, tuple[int, float]] = {}
    assert sut.should_handle_modified(f, fps, now=f.stat().st_mtime + 1)
    assert not sut.should_handle_modified(f, fps, now=f.stat().st_mtime + 2)


def test_modified_skips_old_file_first_sight(tmp_path) -> None:
    f = tmp_path / "old.mkv"
    f.write_bytes(b"old")
    old = time.time() - 3600
    import os
    os.utime(f, (old, old))
    fps: dict[Path, tuple[int, float]] = {}
    assert not sut.should_handle_modified(f, fps, now=time.time())


def test_modified_accepts_recent_file_first_sight(tmp_path) -> None:
    f = tmp_path / "new.mkv"
    f.write_bytes(b"new")
    fps: dict[Path, tuple[int, float]] = {}
    assert sut.should_handle_modified(f, fps, now=time.time())


def test_handler_skips_spurious_modified_on_old_file(tmp_path) -> None:
    f = tmp_path / "old.mkv"
    f.write_bytes(b"old")
    old = time.time() - 3600
    import os
    os.utime(f, (old, old))
    handler, checker = _make_handler(tmp_path)
    handler.on_modified(_event(str(f)))
    checker.enqueue.assert_not_called()
