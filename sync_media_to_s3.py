#!/usr/bin/env python3
"""
sync_media_to_s3.py — Watch local media folders and sync new/changed files to S3.

Source folders  ->  S3 destination
  C:\\Movies\\   ->  s3://park.movies.archive/
  E:\\Movies\\   ->  s3://park.movies.archive/

Relative paths are preserved:
  C:\\Movies\\Action\\foo.mkv  ->  s3://park.movies.archive/Action/foo.mkv

Upload guards:
  1. Temp-extension filter  — .part / .crdownload / .!qb / .tmp / .download
     are never uploaded (skipped in watcher and initial sync).
  2. Stability check        — a file is only uploaded after its size has been
     unchanged for --stable-secs (default: 60) consecutive seconds.
  3. Ignore file            — sync_media_to_s3.ignore (glob and re: patterns)

Dependencies: watchdog (pip install watchdog), AWS CLI (aws configure)
"""

import argparse
import fnmatch
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer


# ── Config ────────────────────────────────────────────────────────────────────

SOURCES: list[tuple[Path, str]] = [
    (Path(r"C:\Movies"),  "s3://park.movies.archive/"),
    (Path(r"E:\Movies"),  "s3://park.movies.archive/"),
]

TEMP_EXTENSIONS: frozenset[str] = frozenset(
    {".part", ".crdownload", ".!qb", ".tmp", ".download"}
)

IGNORE_FILE = Path(__file__).resolve().parent / "sync_media_to_s3.ignore"


# ── Ignore rules ──────────────────────────────────────────────────────────────

class IgnoreRules:
    """Patterns from sync_media_to_s3.ignore (glob and re: lines)."""

    def __init__(self, globs: list[str], regexes: list[re.Pattern[str]]) -> None:
        self._globs = globs
        self._regexes = regexes

    @classmethod
    def empty(cls) -> "IgnoreRules":
        return cls([], [])

    @classmethod
    def load(cls, path: Path = IGNORE_FILE) -> "IgnoreRules":
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
        args: list[str] = []
        for pattern in self._globs:
            args += ["--exclude", pattern]
        return args


def _glob_match(rel: str, pattern: str) -> bool:
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


def should_skip(
    local_root: Path,
    file_path: Path,
    ignore: IgnoreRules,
) -> bool:
    if is_temp(file_path):
        return True
    return is_ignored_by_rules(local_root, file_path, ignore)


# ── Logging ───────────────────────────────────────────────────────────────────

def _ts() -> str:
    return time.strftime("%H:%M:%S")

def info(msg: str) -> None:
    print(f"[{_ts()}]  {msg}")

def step(msg: str) -> None:
    print(f"\n[{_ts()}] [*] {msg}")

def ok(msg: str) -> None:
    print(f"[{_ts()}]  -> {msg}")

def warn(msg: str) -> None:
    print(f"[{_ts()}]  [!] {msg}", file=sys.stderr)


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_temp(path: Path) -> bool:
    return path.suffix.lower() in TEMP_EXTENSIONS


def s3_key(local_root: Path, file_path: Path, s3_prefix: str) -> str:
    rel = file_path.relative_to(local_root).as_posix()
    return s3_prefix.rstrip("/") + "/" + rel


def run_aws(cmd: list[str], dry_run: bool) -> None:
    if dry_run:
        info(f"[dry-run] {' '.join(cmd)}")
        return
    if len(cmd) >= 5 and cmd[1] == "s3":
        if cmd[2] == "cp":
            info(f"uploading  {cmd[3]}  ->  {cmd[4]}")
        elif cmd[2] == "sync":
            info(f"syncing  {cmd[3]}  ->  {cmd[4]}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        warn(f"aws exited {result.returncode}")


def upload_file(
    local_root: Path,
    file_path: Path,
    s3_prefix: str,
    dry_run: bool,
    ignore: IgnoreRules,
) -> None:
    if should_skip(local_root, file_path, ignore):
        return
    dest = s3_key(local_root, file_path, s3_prefix)
    run_aws(["aws", "s3", "cp", str(file_path), dest], dry_run)


# ── Stability checker ─────────────────────────────────────────────────────────

class StabilityChecker:
    """
    Deduplicates file events and uploads each file only after its size has
    been stable for `stable_secs` consecutive seconds.
    """

    def __init__(self, stable_secs: int, dry_run: bool, ignore: IgnoreRules) -> None:
        self._stable_secs = stable_secs
        self._dry_run = dry_run
        self._ignore = ignore
        self._lock = threading.Lock()
        # path → (local_root, s3_prefix, last_size, last_changed_at)
        self._pending: dict[Path, tuple[Path, str, int, float]] = {}
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def enqueue(self, file_path: Path, local_root: Path, s3_prefix: str) -> None:
        try:
            size = file_path.stat().st_size
        except OSError:
            return
        with self._lock:
            existing = self._pending.get(file_path)
            if existing:
                _, _, last_size, last_changed = existing
                if size != last_size:
                    self._pending[file_path] = (local_root, s3_prefix, size, time.monotonic())
                else:
                    # duplicate event (created+modified) — keep stability timer
                    self._pending[file_path] = (local_root, s3_prefix, size, last_changed)
                return
            self._pending[file_path] = (local_root, s3_prefix, size, time.monotonic())
        info(f"queued  {file_path}  ({size:,} B) — waiting {self._stable_secs}s stability")

    def _loop(self) -> None:
        while True:
            time.sleep(2)
            now = time.monotonic()
            ready: list[tuple[Path, Path, str]] = []

            with self._lock:
                for path, (root, prefix, last_size, last_changed) in list(self._pending.items()):
                    try:
                        current_size = path.stat().st_size
                    except OSError:
                        del self._pending[path]
                        continue

                    if current_size != last_size:
                        self._pending[path] = (root, prefix, current_size, now)
                    elif (now - last_changed) >= self._stable_secs:
                        ready.append((path, root, prefix))
                        del self._pending[path]

            for path, root, prefix in ready:
                info(f"stable  {path}")
                upload_file(root, path, prefix, self._dry_run, self._ignore)


def file_fingerprint(path: Path) -> tuple[int, float] | None:
    try:
        st = path.stat()
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def should_handle_modified(
    path: Path,
    fingerprints: dict[Path, tuple[int, float]],
    *,
    now: float | None = None,
    recent_secs: float = 120,
) -> bool:
    """True when a modified event reflects a real content change worth syncing."""
    fp = file_fingerprint(path)
    if fp is None:
        return False
    prev = fingerprints.get(path)
    fingerprints[path] = fp
    if prev == fp:
        return False
    if prev is None:
        age = (now or time.time()) - path.stat().st_mtime
        if age > recent_secs:
            return False
    return True

class MediaHandler(FileSystemEventHandler):
    def __init__(
        self,
        local_root: Path,
        s3_prefix: str,
        checker: StabilityChecker,
        ignore: IgnoreRules,
    ) -> None:
        self._root = local_root
        self._prefix = s3_prefix
        self._checker = checker
        self._ignore = ignore
        self._ignore_logged: set[Path] = set()
        self._fingerprints: dict[Path, tuple[int, float]] = {}

    def _handle(self, path_str: str, *, from_modified: bool = False) -> None:
        path = Path(path_str)
        if not path.is_file():
            return
        if is_temp(path):
            return
        if is_ignored_by_rules(self._root, path, self._ignore):
            if path not in self._ignore_logged:
                self._ignore_logged.add(path)
                info(f"ignored  {path}")
            return
        if from_modified and not should_handle_modified(path, self._fingerprints):
            return
        fp = file_fingerprint(path)
        if fp is not None:
            self._fingerprints[path] = fp
        self._checker.enqueue(path, self._root, self._prefix)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(event.src_path, from_modified=True)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(event.dest_path)


# ── Initial sync ──────────────────────────────────────────────────────────────

def initial_sync(
    local_root: Path,
    s3_prefix: str,
    dry_run: bool,
    ignore: IgnoreRules,
) -> None:
    step(f"Initial sync: {local_root}  →  {s3_prefix}")
    exclude_args: list[str] = []
    for ext in sorted(TEMP_EXTENSIONS):
        exclude_args += ["--exclude", f"*{ext}"]
    exclude_args += ignore.aws_exclude_args()
    run_aws(
        ["aws", "s3", "sync", str(local_root), s3_prefix] + exclude_args,
        dry_run,
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Watch local folders and sync new/changed files to S3."
    )
    p.add_argument(
        "--no-initial-sync",
        action="store_true",
        help="Skip the startup aws s3 sync.",
    )
    p.add_argument(
        "--stable-secs",
        type=int,
        default=60,
        metavar="N",
        help="Seconds of size-stability required before uploading (default: 60).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print aws commands without executing them.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    ignore = IgnoreRules.load()
    if ignore._globs or ignore._regexes:
        info(f"Loaded {len(ignore._globs)} glob + {len(ignore._regexes)} regex ignore rules from {IGNORE_FILE.name}")

    active_sources = [(root, prefix) for root, prefix in SOURCES if root.is_dir()]
    missing = [root for root, _ in SOURCES if not root.is_dir()]
    for m in missing:
        warn(f"Source folder not found, skipping: {m}")
    if not active_sources:
        warn("No source folders available. Exiting.")
        sys.exit(1)

    if not args.no_initial_sync:
        for root, prefix in active_sources:
            initial_sync(root, prefix, args.dry_run, ignore)

    checker = StabilityChecker(args.stable_secs, args.dry_run, ignore)
    observer = Observer()

    step("Starting file watcher")
    for root, prefix in active_sources:
        handler = MediaHandler(root, prefix, checker, ignore)
        observer.schedule(handler, str(root), recursive=True)
        info(f"watching  {root}  →  {prefix}")

    observer.start()
    info("\nPress Ctrl+C to stop.\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
        info("Stopped.")


if __name__ == "__main__":
    main()
