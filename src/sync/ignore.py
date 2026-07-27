"""Ignore rules for sync (glob and re: patterns)."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path


class IgnoreRules:
    """Patterns from sync.ignore (glob and re: lines)."""

    def __init__(self, globs: list[str], regexes: list[re.Pattern[str]]) -> None:
        self._globs = globs
        self._regexes = regexes

    @classmethod
    def empty(cls) -> IgnoreRules:
        return cls([], [])

    @classmethod
    def load(cls, path: Path) -> IgnoreRules:
        globs: list[str] = []
        regexes: list[re.Pattern[str]] = []
        if not path.is_file():
            return cls(globs, regexes)
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("re:"):
                regexes.append(re.compile(line[3:].strip()))
            else:
                globs.append(line)
        return cls(globs, regexes)

    def with_extra_globs(self, patterns: list[str]) -> IgnoreRules:
        """Return a copy with additional glob/folder exclude patterns."""
        extra = [p.replace("\\", "/").strip().rstrip("/") for p in patterns if p.strip()]
        return IgnoreRules([*self._globs, *extra], list(self._regexes))

    def is_ignored(self, rel_path: str) -> bool:
        rel = rel_path.replace("\\", "/")
        for pattern in self._globs:
            if _glob_match(rel, pattern):
                return True
        for rx in self._regexes:
            if rx.search(rel):
                return True
        return False

    def aws_exclude_args(self) -> list[str]:
        """Build aws s3 sync --exclude args (folder names expand to dir/*)."""
        args: list[str] = []
        seen: set[str] = set()
        for pattern in self._globs:
            for excl in _aws_exclude_patterns(pattern):
                if excl not in seen:
                    seen.add(excl)
                    args += ["--exclude", excl]
        return args


def _is_plain_name(pattern: str) -> bool:
    return "*" not in pattern and "?" not in pattern and not pattern.startswith("re:")


def _aws_exclude_patterns(pattern: str) -> list[str]:
    pattern = pattern.replace("\\", "/").strip().rstrip("/")
    if not pattern:
        return []
    if _is_plain_name(pattern):
        # Exclude the directory itself and everything under it
        return [pattern, f"{pattern}/*", f"{pattern}/**"]
    return [pattern]


def _glob_match(rel: str, pattern: str) -> bool:
    pattern = pattern.replace("\\", "/").rstrip("/")
    if _is_plain_name(pattern):
        # "TV" matches "TV" and "TV/anything/..."
        return rel == pattern or rel.startswith(pattern + "/")
    if pattern.startswith("**/"):
        suffix = pattern[3:]
        if fnmatch.fnmatch(rel, suffix):
            return True
        parts = rel.split("/")
        for i in range(len(parts)):
            if fnmatch.fnmatch("/".join(parts[i:]), suffix):
                return True
        return False
    return fnmatch.fnmatch(rel, pattern)


def is_ignored_by_rules(
    local_root: Path,
    file_path: Path,
    ignore: IgnoreRules,
) -> bool:
    try:
        rel = file_path.relative_to(local_root).as_posix()
    except ValueError:
        return False
    return ignore.is_ignored(rel)
