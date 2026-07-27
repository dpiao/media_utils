"""Shared paths for the supervisor."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
LOG_FILE = REPO_ROOT / "mediactl.log"
LOGS_DIR = REPO_ROOT / "logs"
APP_NAME = "mediactl"
USER_CONFIG_DIR = Path.home() / ".config" / "media_utils"
CONTROL_SOCK = USER_CONFIG_DIR / "mediactl.sock"
