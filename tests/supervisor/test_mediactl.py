"""Tests for supervisor (mediactl)."""

from __future__ import annotations

import inspect
import queue
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from supervisor.app import TrayApp
from supervisor import log_coalesce as lc
from supervisor.platform import windows as win_plat
from supervisor.status_window import StatusWindow
from supervisor.worker import WorkerProcess, worker_python

win32_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only")


def test_python_exe_is_not_pythonw():
    assert not worker_python().lower().endswith("pythonw.exe")


def test_python_exe_substitution():
    fake_pythonw = r"C:\Python311\pythonw.exe"
    with patch.object(sys, "executable", fake_pythonw):
        result = worker_python()
    assert result == r"C:\Python311\python.exe"


def _make_worker(notify_cb=None):
    cfg = {
        "name": "Test",
        "cmd": ["python", "-c", "pass"],
        "cwd": Path("."),
        "notifications": [
            {"prefix": "NOTIFY:Render complete", "title": "Render complete"},
            {"prefix": "NOTIFY:Render failed", "title": "Render failed"},
        ],
    }
    return WorkerProcess(cfg, queue.Queue(), notify_cb or (lambda t, b: None))


def test_dispatch_notify_with_body():
    calls = []
    w = _make_worker(lambda t, b: calls.append((t, b)))
    w._dispatch("NOTIFY:Render complete|my_video.mp4")
    assert calls == [("Render complete", "my_video.mp4")]


def test_dispatch_notify_no_body():
    calls = []
    w = _make_worker(lambda t, b: calls.append((t, b)))
    w._dispatch("NOTIFY:Render complete")
    assert calls == [("Render complete", "")]


def test_dispatch_no_match():
    calls = []
    w = _make_worker(lambda t, b: calls.append((t, b)))
    w._dispatch("some random log line")
    assert calls == []


def test_dispatch_partial_prefix_not_matched():
    calls = []
    w = _make_worker(lambda t, b: calls.append((t, b)))
    w._dispatch("NOTIFY:Render completeness check")
    assert calls == []


def test_dispatch_second_pattern():
    calls = []
    w = _make_worker(lambda t, b: calls.append((t, b)))
    w._dispatch("NOTIFY:Render failed|folder_x: exiftool error")
    assert calls == [("Render failed", "folder_x: exiftool error")]


def _fake_popen(lines: list[str], returncode: int = 0):
    proc = MagicMock()
    proc.pid = 99999
    proc.returncode = returncode
    proc.poll.return_value = None
    proc.stdout = iter(line + "\n" for line in lines)
    proc.wait.return_value = returncode
    return proc


def test_worker_not_running_initially():
    assert not _make_worker().running


def test_worker_start_sets_running():
    w = _make_worker()
    fake_proc = _fake_popen([])
    with patch("subprocess.Popen", return_value=fake_proc):
        w.start()
    assert w.running
    assert w.pid == 99999


def test_worker_start_idempotent():
    w = _make_worker()
    fake_proc = _fake_popen([])
    with patch("subprocess.Popen", return_value=fake_proc) as mock_popen:
        w.start()
        w.start()
    mock_popen.assert_called_once()


def test_worker_stop_sets_not_running():
    w = _make_worker()
    fake_proc = _fake_popen([])
    with patch("subprocess.Popen", return_value=fake_proc):
        w.start()

    def _terminate():
        fake_proc.poll.return_value = 1

    fake_proc.terminate.side_effect = _terminate
    w.stop()
    assert not w.running
    assert w.uptime == "—"
    fake_proc.terminate.assert_called_once()


def test_worker_stop_without_start_is_safe():
    _make_worker().stop()


def test_pid_none_before_start():
    assert _make_worker().pid is None


def test_uptime_running():
    w = _make_worker()
    fake_proc = _fake_popen([])
    with patch("subprocess.Popen", return_value=fake_proc):
        w.start()
    w._start_time = time.monotonic() - 90
    assert w.uptime == "00:01:30"


def test_read_loop_puts_lines_in_log_queue():
    log_q = queue.Queue()
    cfg = {
        "name": "Test",
        "cmd": ["python", "-c", "pass"],
        "cwd": Path("."),
        "notifications": [],
    }
    w = WorkerProcess(cfg, log_q, lambda t, b: None)
    fake_proc = _fake_popen(["hello", "world"])
    with patch("subprocess.Popen", return_value=fake_proc):
        w._stopped = True
        w._proc = fake_proc
        w._read_loop()

    lines_received = []
    while not log_q.empty():
        _, line, _mode = log_q.get_nowait()
        lines_received.append(line)
    assert "hello" in lines_received
    assert "world" in lines_received


def test_read_loop_fires_notify():
    notified = []
    cfg = {
        "name": "Test",
        "cmd": ["python", "-c", "pass"],
        "cwd": Path("."),
        "notifications": [{"prefix": "NOTIFY:Done", "title": "All done"}],
    }
    w = WorkerProcess(cfg, queue.Queue(), lambda t, b: notified.append((t, b)))
    fake_proc = _fake_popen(["NOTIFY:Done|result.mp4"])
    fake_proc.poll.return_value = 0
    w._stopped = True
    w._proc = fake_proc
    w._read_loop()
    assert ("All done", "result.mp4") in notified


def test_run_in_thread_has_two_params():
    action = TrayApp._run_in_thread(lambda: None)
    params = list(inspect.signature(action).parameters.keys())
    assert params == ["icon", "item"]


def test_run_in_thread_calls_fn_in_thread():
    done = threading.Event()

    def fn():
        done.set()

    TrayApp._run_in_thread(fn)(None, None)
    assert done.wait(timeout=2)


def test_status_window_builds_without_display():
    workers = [_make_worker(), _make_worker()]
    workers[1].name = "Other"
    win = StatusWindow(workers, queue.Queue(), on_quit=lambda: None, on_autostart_toggle=lambda _: None)
    assert win._log_widgets == {}


@patch("supervisor.status_window.tk.Tk")
@patch("supervisor.status_window.plat.is_autostart_enabled", return_value=False)
def test_status_window_show_creates_root(mock_autostart, mock_tk):
    mock_root = MagicMock()
    mock_root.winfo_exists.return_value = True
    mock_tk.return_value = mock_root
    workers = [_make_worker()]
    win = StatusWindow(workers, queue.Queue(), on_quit=lambda: None, on_autostart_toggle=lambda _: None)
    with patch.object(win, "_build") as mock_build:
        win.show()
        mock_build.assert_called_once()
    win._root = mock_root
    win.show()
    mock_root.deiconify.assert_called_once()
    mock_root.lift.assert_called_once()


@win32_only
def test_windows_workers_include_render_and_sync():
    names = [w["name"] for w in win_plat.get_workers()]
    assert names == ["Render VR360", "S3 Sync"]


def test_workers_progress_patterns_compile() -> None:
    for cfg in win_plat.get_workers():
        for pat in cfg.get("progress_patterns", []):
            __import__("re").compile(pat)


def test_coalescer_ffmpeg_progress_replace() -> None:
    c = lc.LogCoalescer([r"^\s*(hevc_nvenc|libx265)\s+\["])
    line = "  hevc_nvenc  [####-----]  40.0%  frame 100/250  25.0 fps  ETA 00:06"
    assert c.process(line) == ("append", True)
    assert c.process(line) == ("replace", True)
    assert c.process("[*] Encoding done") == ("append", False)


def test_coalescer_aws_progress_replace() -> None:
    c = lc.LogCoalescer([r"^Completed .+ with .+ remaining"])
    line = "Completed 58.0 GiB/58.0 GiB (4.6 MiB/s) with 1 file(s) remaining"
    assert c.process(line) == ("append", True)
    assert c.process(line) == ("replace", True)


def test_coalescer_resets_after_normal_line() -> None:
    c = lc.LogCoalescer([r"^\s*(hevc_nvenc|libx265)\s+\["])
    prog = "  libx265  [##-------]  10.0%  frame 10/100  20.0 fps  ETA 00:04"
    c.process(prog)
    c.process(prog)
    c.process("normal log line")
    assert c.process(prog) == ("append", True)


def test_progress_file_throttle_interval() -> None:
    throttle = lc.ProgressFileThrottle()
    line = "Completed 1.0 GiB/10.0 GiB (1.0 MiB/s) with 1 file(s) remaining"
    assert throttle.note_replace(line) == line
    assert throttle.note_replace(line) is None
    throttle._last_write_at = time.monotonic() - lc.FILE_PROGRESS_INTERVAL_SEC - 1
    assert throttle.note_replace(line) == line


def test_progress_file_throttle_flush_before_normal() -> None:
    throttle = lc.ProgressFileThrottle()
    line = "  hevc_nvenc  [##########]  99.0%  frame 99/100  25.0 fps  ETA 00:00"
    throttle.note_replace(line)
    assert throttle.flush_before_normal() == line
    assert throttle.flush_before_normal() is None


@win32_only
def test_single_instance_exits_when_mutex_taken():
    import ctypes

    from supervisor.platform import windows as win

    with patch.object(ctypes.windll.kernel32, "CreateMutexW"), patch.object(
        ctypes.windll.kernel32, "GetLastError", return_value=183
    ):
        with pytest.raises(SystemExit) as exc_info:
            win.acquire_single_instance()
    assert exc_info.value.code == 0


@win32_only
def test_is_autostart_enabled_true():
    from supervisor.platform import windows as win

    with patch("winreg.OpenKey") as mock_open, patch("winreg.QueryValueEx"):
        mock_open.return_value.__enter__ = lambda s: s
        mock_open.return_value.__exit__ = MagicMock(return_value=False)
        assert win.is_autostart_enabled() is True


@darwin_only
def test_macos_workers_sync_only():
    from supervisor.platform import macos as mac

    workers = mac.get_workers()
    assert len(workers) == 1
    assert workers[0]["name"] == "S3 Sync"
    assert workers[0]["cmd"][-2:] == ["-m", "sync"]


@darwin_only
def test_macos_plist_contains_supervisor_and_pythonpath():
    from supervisor.platform import macos as mac

    body = mac._plist_body()
    assert mac.LAUNCH_AGENT_LABEL in body
    assert "-m" in body
    assert "supervisor" in body
    assert "PYTHONPATH" in body


@darwin_only
def test_macos_second_instance_lock_contended(tmp_path, monkeypatch):
    import fcntl

    from supervisor.platform import macos as mac

    lock = tmp_path / "mediactl.lock"
    monkeypatch.setattr(mac, "USER_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(mac, "LOCK_FILE", lock)

    mac.acquire_single_instance()
    with open(lock, "a", encoding="utf-8") as fh:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


@darwin_only
def test_macos_set_autostart_writes_plist(tmp_path, monkeypatch):
    from supervisor.platform import macos as mac

    plist = tmp_path / "LaunchAgents" / f"{mac.LAUNCH_AGENT_LABEL}.plist"
    log_dir = tmp_path / "Logs"
    monkeypatch.setattr(mac, "LAUNCH_AGENT_PLIST", plist)
    monkeypatch.setattr(mac, "LAUNCHD_LOG_DIR", log_dir)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mac.set_autostart(True)
        assert plist.is_file()
        assert "supervisor" in plist.read_text(encoding="utf-8")
        mac.set_autostart(False)
        assert not plist.is_file()
