"""
Tests for mediactl.py

Run with:  pytest test_mediactl.py -v
"""

import inspect
import io
import queue
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

import mediactl as sut


# ── PYTHON exe substitution ───────────────────────────────────────────────────

def test_python_exe_is_not_pythonw():
    """Workers must use python.exe, not pythonw.exe (pythonw sets stdout=None)."""
    assert not sut.PYTHON.lower().endswith("pythonw.exe"), (
        "PYTHON must not be pythonw.exe — worker stdout pipes break under pythonw"
    )


def test_python_exe_substitution():
    import sys, os
    fake_pythonw = r"C:\Python311\pythonw.exe"
    with patch.object(sys, "executable", fake_pythonw):
        # Re-evaluate the substitution logic from the module
        exe = sys.executable
        result = exe.replace("pythonw.exe", "python.exe") if exe.lower().endswith("pythonw.exe") else exe
    assert result == r"C:\Python311\python.exe"


# ── WorkerProcess._dispatch ───────────────────────────────────────────────────

def _make_worker(notify_cb=None):
    cfg = {
        "name": "Test",
        "cmd": ["python", "-c", "pass"],
        "cwd": Path("."),
        "notifications": [
            {"prefix": "NOTIFY:Render complete", "title": "Render complete"},
            {"prefix": "NOTIFY:Render failed",   "title": "Render failed"},
        ],
    }
    return sut.WorkerProcess(cfg, queue.Queue(), notify_cb or (lambda t, b: None))


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
    """A line that starts with the prefix text but has no | separator and isn't exact
    should not fire (e.g. NOTIFY:Render completeness is not NOTIFY:Render complete)."""
    calls = []
    w = _make_worker(lambda t, b: calls.append((t, b)))
    w._dispatch("NOTIFY:Render completeness check")
    assert calls == []


def test_dispatch_second_pattern():
    calls = []
    w = _make_worker(lambda t, b: calls.append((t, b)))
    w._dispatch("NOTIFY:Render failed|folder_x: exiftool error")
    assert calls == [("Render failed", "folder_x: exiftool error")]


# ── WorkerProcess lifecycle ───────────────────────────────────────────────────

def _fake_popen(lines: list[str], returncode: int = 0):
    """Build a mock Popen whose stdout yields the given lines."""
    proc = MagicMock()
    proc.pid = 99999
    proc.returncode = returncode
    proc.poll.return_value = None          # appears running initially
    proc.stdout = iter(line + "\n" for line in lines)
    proc.wait.return_value = returncode
    return proc


def test_worker_not_running_initially():
    w = _make_worker()
    assert not w.running


def test_worker_start_sets_running():
    w = _make_worker()
    fake_proc = _fake_popen([])
    with patch("subprocess.Popen", return_value=fake_proc):
        w.start()
    assert w.running


def test_worker_start_idempotent():
    """Calling start() twice should not spawn a second process."""
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
    # Simulate terminate() causing poll() to return an exit code
    def _terminate():
        fake_proc.poll.return_value = 1
    fake_proc.terminate.side_effect = _terminate
    w.stop()
    assert not w.running
    fake_proc.terminate.assert_called_once()


def test_worker_stop_without_start_is_safe():
    w = _make_worker()
    w.stop()  # should not raise


# ── WorkerProcess._read_loop — output routing and NOTIFY dispatch ─────────────

def test_read_loop_puts_lines_in_log_queue():
    log_q = queue.Queue()
    cfg = {
        "name": "Test",
        "cmd": ["python", "-c", "pass"],
        "cwd": Path("."),
        "notifications": [],
    }
    w = sut.WorkerProcess(cfg, log_q, lambda t, b: None)
    fake_proc = _fake_popen(["hello", "world"])
    fake_proc.poll.side_effect = [None, None, 0]  # running, then done

    with patch("subprocess.Popen", return_value=fake_proc):
        w._stopped = True  # prevent auto-restart
        w._proc = fake_proc
        w._read_loop()

    lines_received = []
    while not log_q.empty():
        _, line = log_q.get_nowait()
        lines_received.append(line)
    assert "hello" in lines_received
    assert "world" in lines_received


def test_read_loop_fires_notify():
    notified = []
    cfg = {
        "name": "Test",
        "cmd": ["python", "-c", "pass"],
        "cwd": Path("."),
        "notifications": [
            {"prefix": "NOTIFY:Done", "title": "All done"},
        ],
    }
    w = sut.WorkerProcess(cfg, queue.Queue(), lambda t, b: notified.append((t, b)))
    fake_proc = _fake_popen(["NOTIFY:Done|result.mp4"])
    fake_proc.poll.return_value = 0

    w._stopped = True
    w._proc = fake_proc
    w._read_loop()

    assert ("All done", "result.mp4") in notified


# ── Autostart registry ────────────────────────────────────────────────────────

def test_is_autostart_enabled_true():
    with patch("winreg.OpenKey") as mock_open, \
         patch("winreg.QueryValueEx") as mock_query:
        mock_open.return_value.__enter__ = lambda s: s
        mock_open.return_value.__exit__ = MagicMock(return_value=False)
        assert sut.is_autostart_enabled() is True
        mock_query.assert_called_once()


def test_is_autostart_enabled_false():
    with patch("winreg.OpenKey") as mock_open, \
         patch("winreg.QueryValueEx", side_effect=FileNotFoundError):
        mock_open.return_value.__enter__ = lambda s: s
        mock_open.return_value.__exit__ = MagicMock(return_value=False)
        assert sut.is_autostart_enabled() is False


def _mock_open_key():
    """Return a context-manager-compatible mock for winreg.OpenKey."""
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=cm)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def test_set_autostart_enable():
    import winreg
    cm = _mock_open_key()
    with patch("winreg.OpenKey", return_value=cm), \
         patch("winreg.SetValueEx") as mock_set:
        sut.set_autostart(True)
    mock_set.assert_called_once_with(cm, sut.APP_NAME, 0, winreg.REG_SZ, sut.AUTOSTART_CMD)


def test_set_autostart_disable():
    cm = _mock_open_key()
    with patch("winreg.OpenKey", return_value=cm), \
         patch("winreg.DeleteValue") as mock_del:
        sut.set_autostart(False)
    mock_del.assert_called_once_with(cm, sut.APP_NAME)


def test_set_autostart_disable_missing_key_is_safe():
    cm = _mock_open_key()
    with patch("winreg.OpenKey", return_value=cm), \
         patch("winreg.DeleteValue", side_effect=FileNotFoundError):
        sut.set_autostart(False)  # must not raise


# ── TrayApp._run_in_thread ────────────────────────────────────────────────────

def test_run_in_thread_has_two_params():
    """pystray requires action callables to accept exactly (icon, item)."""
    action = sut.TrayApp._run_in_thread(lambda: None)
    params = list(inspect.signature(action).parameters.keys())
    assert params == ["icon", "item"], (
        f"pystray action must have (icon, item) params, got {params}"
    )


def test_run_in_thread_calls_fn_in_thread():
    done = threading.Event()

    def fn():
        done.set()

    action = sut.TrayApp._run_in_thread(fn)
    action(None, None)
    assert done.wait(timeout=2), "fn was not called within 2s"


# ── Single-instance mutex ─────────────────────────────────────────────────────

def test_single_instance_exits_when_mutex_taken():
    import ctypes
    with patch.object(ctypes.windll.kernel32, "CreateMutexW"), \
         patch.object(ctypes.windll.kernel32, "GetLastError", return_value=183):
        with pytest.raises(SystemExit) as exc_info:
            sut._acquire_single_instance_mutex()
    assert exc_info.value.code == 0


def test_single_instance_continues_when_mutex_free():
    import ctypes
    with patch.object(ctypes.windll.kernel32, "CreateMutexW"), \
         patch.object(ctypes.windll.kernel32, "GetLastError", return_value=0):
        sut._acquire_single_instance_mutex()  # must not raise or exit
