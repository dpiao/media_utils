"""Windows: named mutex, HKCU Run autostart, VR360 + S3 Sync workers."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import winreg

from supervisor.paths import APP_NAME, REPO_ROOT, SRC_DIR
from supervisor.worker import worker_python

_log = logging.getLogger("mediactl")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
PYTHON = worker_python()
PYTHONW = Path(PYTHON).with_name("pythonw.exe")
# Prefer -m supervisor so PYTHONPATH=src works at login
AUTOSTART_CMD = (
    f'"{PYTHONW}" "-c" '
    f'"import sys; sys.path.insert(0, r\'{SRC_DIR}\'); '
    f'from supervisor.app import main; main()"'
)


def acquire_single_instance() -> None:
    import ctypes

    mutex_name = "Global\\mediactl_singleton"
    ctypes.windll.kernel32.CreateMutexW(None, True, mutex_name)
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit(0)


def is_autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except FileNotFoundError:
        return False


def get_autostart_cmd() -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return value
    except FileNotFoundError:
        return None


def set_autostart(enable: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, access=winreg.KEY_SET_VALUE) as key:
        if enable:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, AUTOSTART_CMD)
            _log.info("Autostart enabled: %s", AUTOSTART_CMD)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
                _log.info("Autostart disabled")
            except FileNotFoundError:
                pass


def repair_autostart_if_needed() -> bool:
    if not is_autostart_enabled():
        return False
    stored = get_autostart_cmd()
    if stored == AUTOSTART_CMD:
        return False
    _log.info("Autostart path outdated (%s) — updating to %s", stored, AUTOSTART_CMD)
    set_autostart(True)
    return True


def get_workers() -> list[dict]:
    python = worker_python()
    windows_dir = SRC_DIR / "windows"
    sync_notifications = [
        {"prefix": "NOTIFY:Upload pending", "title": "S3 upload pending"},
        {"prefix": "NOTIFY:Uploaded to S3", "title": "S3 upload complete"},
        {"prefix": "NOTIFY:Warning", "title": "S3 Sync warning"},
    ]
    return [
        {
            "name": "Render VR360",
            "cmd": [python, str(windows_dir / "render_vr360.py")],
            "cwd": windows_dir,
            "notifications": [
                {"prefix": "NOTIFY:Render started", "title": "Render started"},
                {"prefix": "NOTIFY:Render complete", "title": "Render complete"},
                {"prefix": "NOTIFY:Render failed", "title": "Render failed"},
            ],
            "progress_patterns": [
                r"^\s*(hevc_nvenc|libx265)\s+\[",
            ],
        },
        {
            "name": "S3 Sync",
            "cmd": [python, "-m", "sync"],
            "cwd": REPO_ROOT,
            "notifications": sync_notifications,
            "progress_patterns": [
                r"^Completed .+ with .+ remaining",
            ],
        },
    ]


def log_startup_diagnostics() -> None:
    _log.info("Platform: Windows")
    _log.info("Python: %s", worker_python())
    _log.info("PYTHONPATH (workers): %s", SRC_DIR)
    _log.info("Repo root: %s", REPO_ROOT)
    _log.info("Autostart enabled: %s", is_autostart_enabled())
    for w in get_workers():
        _log.info("Worker configured: %s -> %s", w["name"], w["cmd"])
