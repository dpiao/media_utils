"""macOS: flock single-instance, LaunchAgent autostart, S3 Sync only."""

from __future__ import annotations

import fcntl
import logging
import os
import subprocess
import sys
from pathlib import Path

from supervisor.paths import REPO_ROOT, SRC_DIR
from supervisor.worker import worker_python
from sync import config as sync_config

_log = logging.getLogger("mediactl")

LAUNCH_AGENT_LABEL = "com.media_utils.mediactl"
LAUNCH_AGENT_PLIST = (
    Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
)
USER_CONFIG_DIR = Path.home() / ".config" / "media_utils"
LOCK_FILE = USER_CONFIG_DIR / "mediactl.lock"
LAUNCHD_LOG_DIR = Path.home() / "Library" / "Logs" / "mediactl"

_lock_fh = None


def acquire_single_instance() -> None:
    global _lock_fh
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _lock_fh = open(LOCK_FILE, "w", encoding="utf-8")
    try:
        fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _log.info("Another mediactl instance holds %s — exiting", LOCK_FILE)
        sys.exit(0)
    _lock_fh.write(str(os.getpid()))
    _lock_fh.flush()
    _log.info("Single-instance lock acquired: %s", LOCK_FILE)


def _preferred_python() -> str:
    venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    if venv_python.is_file():
        return str(venv_python)
    return worker_python()


def _plist_body() -> str:
    python = _preferred_python()
    out_log = LAUNCHD_LOG_DIR / "launchd.out.log"
    err_log = LAUNCHD_LOG_DIR / "launchd.err.log"
    # XML plist with RunAtLoad
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LAUNCH_AGENT_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>-m</string>
    <string>supervisor</string>
  </array>
  <key>WorkingDirectory</key>
  <string>{REPO_ROOT}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key>
    <string>{SRC_DIR}</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{out_log}</string>
  <key>StandardErrorPath</key>
  <string>{err_log}</string>
</dict>
</plist>
"""


def is_autostart_enabled() -> bool:
    return LAUNCH_AGENT_PLIST.is_file()


def set_autostart(enable: bool) -> None:
    uid = os.getuid()
    domain = f"gui/{uid}/{LAUNCH_AGENT_LABEL}"
    if enable:
        LAUNCHD_LOG_DIR.mkdir(parents=True, exist_ok=True)
        LAUNCH_AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
        LAUNCH_AGENT_PLIST.write_text(_plist_body(), encoding="utf-8")
        # bootout if already loaded, then bootstrap
        subprocess.run(
            ["launchctl", "bootout", domain],
            capture_output=True,
            check=False,
        )
        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(LAUNCH_AGENT_PLIST)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            # fallback for older macOS
            subprocess.run(
                ["launchctl", "load", "-w", str(LAUNCH_AGENT_PLIST)],
                capture_output=True,
                check=False,
            )
        _log.info("Autostart enabled: %s", LAUNCH_AGENT_PLIST)
    else:
        subprocess.run(
            ["launchctl", "bootout", domain],
            capture_output=True,
            check=False,
        )
        subprocess.run(
            ["launchctl", "unload", "-w", str(LAUNCH_AGENT_PLIST)],
            capture_output=True,
            check=False,
        )
        if LAUNCH_AGENT_PLIST.is_file():
            LAUNCH_AGENT_PLIST.unlink()
        _log.info("Autostart disabled (removed %s)", LAUNCH_AGENT_PLIST)


def repair_autostart_if_needed() -> bool:
    if not is_autostart_enabled():
        return False
    expected = _plist_body()
    current = LAUNCH_AGENT_PLIST.read_text(encoding="utf-8")
    if current == expected:
        return False
    _log.info("Autostart plist outdated — rewriting %s", LAUNCH_AGENT_PLIST)
    set_autostart(True)
    return True


def get_workers() -> list[dict]:
    python = _preferred_python()
    return [
        {
            "name": "S3 Sync",
            "cmd": [python, "-m", "sync"],
            "cwd": REPO_ROOT,
            "notifications": [
                {"prefix": "NOTIFY:Upload pending", "title": "S3 upload pending"},
                {"prefix": "NOTIFY:Uploaded to S3", "title": "S3 upload complete"},
                {"prefix": "NOTIFY:Warning", "title": "S3 Sync warning"},
            ],
        },
    ]


def log_startup_diagnostics() -> None:
    _log.info("Platform: macOS")
    _log.info("Python: %s", worker_python())
    _log.info("PYTHONPATH (workers): %s", SRC_DIR)
    _log.info("Repo root: %s", REPO_ROOT)
    _log.info("Lock file: %s", LOCK_FILE)
    _log.info("LaunchAgent plist: %s", LAUNCH_AGENT_PLIST)
    _log.info("Autostart enabled: %s", is_autostart_enabled())
    try:
        cfg = sync_config.ensure_user_config()
        _log.info("Sync config: %s", cfg)
        for root, prefix in sync_config.load_sources(cfg):
            _log.info("Sync source: %s → %s", root, prefix)
    except Exception as exc:
        _log.warning("Could not load sync config: %s", exc)
    for w in get_workers():
        _log.info("Worker configured: %s -> %s", w["name"], w["cmd"])
