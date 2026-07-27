"""Load per-OS sync config and ignore files from the repo config/ directory."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"

# Legacy locations under ~/.config/media_utils/ (pre repo-local configs).
HOME_CONFIG_DIR = Path.home() / ".config" / "media_utils"


def platform_tag() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    return "macos"


def user_sync_toml() -> Path:
    return CONFIG_DIR / f"sync.{platform_tag()}.toml"


def user_ignore() -> Path:
    return CONFIG_DIR / f"sync.{platform_tag()}.ignore"


def _legacy_sync_candidates() -> list[Path]:
    tag = platform_tag()
    return [
        HOME_CONFIG_DIR / f"sync.{tag}.toml",
        HOME_CONFIG_DIR / "sync.toml",
        CONFIG_DIR / f"sync.example.{tag}.toml",
    ]


def _legacy_ignore_candidates() -> list[Path]:
    tag = platform_tag()
    return [
        HOME_CONFIG_DIR / f"sync.{tag}.ignore",
        HOME_CONFIG_DIR / "sync.ignore",
        CONFIG_DIR / "sync.ignore.example",
    ]


def ensure_user_config() -> Path:
    """Return repo ``config/sync.<os>.toml``, migrating from legacy paths if needed."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = user_sync_toml()
    if path.is_file():
        return path
    for candidate in _legacy_sync_candidates():
        if candidate.is_file():
            shutil.copyfile(candidate, path)
            return path
    raise FileNotFoundError(
        f"Missing sync config: {path} (expected under {CONFIG_DIR})",
    )


def resolve_ignore_path() -> Path:
    """Return repo ``config/sync.<os>.ignore``, migrating from legacy paths if needed."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = user_ignore()
    if path.is_file():
        return path
    for candidate in _legacy_ignore_candidates():
        if candidate.is_file():
            shutil.copyfile(candidate, path)
            return path
    raise FileNotFoundError(
        f"Missing ignore file: {path} (expected under {CONFIG_DIR})",
    )


def _load_toml(config_path: Path) -> dict:
    return tomllib.loads(config_path.read_text(encoding="utf-8"))


def load_sources(config_path: Path | None = None) -> list[tuple[Path, str]]:
    """Return (expanded local path, s3 prefix) pairs from TOML."""
    path = config_path or ensure_user_config()
    data = _load_toml(path)
    sources_raw = data.get("sources") or []
    if not sources_raw:
        raise ValueError(f"No [[sources]] entries in {path}")
    result: list[tuple[Path, str]] = []
    for entry in sources_raw:
        local = Path(entry["path"]).expanduser().resolve()
        s3 = str(entry["s3"]).strip()
        if not s3:
            raise ValueError(f"Empty s3 prefix in {path}")
        result.append((local, s3))
    return result


def load_excludes(config_path: Path | None = None) -> list[str]:
    """
    Folder/file exclude patterns from sync.toml.

    Supports top-level `exclude = [...]` and per-source `exclude = [...]`.
    Plain names (e.g. \"TV\") match that path and everything under it.
    """
    path = config_path or ensure_user_config()
    data = _load_toml(path)
    excludes: list[str] = []
    for item in data.get("exclude") or []:
        excludes.append(str(item))
    for entry in data.get("sources") or []:
        for item in entry.get("exclude") or []:
            excludes.append(str(item))
    # de-dupe, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for e in excludes:
        key = e.replace("\\", "/").strip().rstrip("/")
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out
