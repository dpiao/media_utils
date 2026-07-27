#!/usr/bin/env python3
"""Watch local media folders and sync new/changed files to S3."""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from sync import config as sync_config
from sync.ignore import IgnoreRules, is_ignored_by_rules

TEMP_EXTENSIONS: frozenset[str] = frozenset(
    {".part", ".crdownload", ".!qb", ".tmp", ".download", ".aac", ".m4v"}
)


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
    print(f"NOTIFY:Warning|{msg}", flush=True)


def is_temp(path: Path) -> bool:
    return path.suffix.lower() in TEMP_EXTENSIONS


def should_skip(local_root: Path, file_path: Path, ignore: IgnoreRules) -> bool:
    if is_temp(file_path):
        return True
    return is_ignored_by_rules(local_root, file_path, ignore)


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
    try:
        result = subprocess.run(cmd)
    except FileNotFoundError:
        warn("aws CLI not found on PATH — install AWS CLI or fix PATH")
        return
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


class StabilityChecker:
    """Upload each file only after size is stable for stable_secs."""

    def __init__(self, stable_secs: int, dry_run: bool, ignore: IgnoreRules) -> None:
        self._stable_secs = stable_secs
        self._dry_run = dry_run
        self._ignore = ignore
        self._lock = threading.Lock()
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
                    self._pending[file_path] = (local_root, s3_prefix, size, last_changed)
                return
            self._pending[file_path] = (local_root, s3_prefix, size, time.monotonic())
        info(f"queued  {file_path}  ({size:,} B) — waiting {self._stable_secs}s stability")
        print(f"NOTIFY:Upload pending|{file_path.name}", flush=True)

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
                print(f"NOTIFY:Uploaded to S3|{path.name}", flush=True)


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


def initial_sync(
    local_root: Path,
    s3_prefix: str,
    dry_run: bool,
    ignore: IgnoreRules,
) -> None:
    """
    Sync each top-level child separately so excluded / unreadable folders
    (e.g. TV) are never entered by aws.
    """
    step(f"Initial sync: {local_root}  →  {s3_prefix}")
    exclude_args: list[str] = []
    for ext in sorted(TEMP_EXTENSIONS):
        exclude_args += ["--exclude", f"*{ext}"]
    exclude_args += ignore.aws_exclude_args()

    try:
        children = sorted(local_root.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        warn(f"Cannot list {local_root}: {exc}")
        return

    for child in children:
        name = child.name
        if ignore.is_ignored(name):
            info(f"skipping excluded  {child}")
            continue
        if child.is_dir():
            dest = s3_prefix.rstrip("/") + "/" + name
            run_aws(
                ["aws", "s3", "sync", str(child), dest] + exclude_args,
                dry_run,
            )
        elif child.is_file():
            if should_skip(local_root, child, ignore):
                continue
            dest = s3_key(local_root, child, s3_prefix)
            run_aws(["aws", "s3", "cp", str(child), dest], dry_run)


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
    config_path = sync_config.ensure_user_config()
    info(f"Config: {config_path}")
    sources = sync_config.load_sources(config_path)
    for root, prefix in sources:
        info(f"Configured  {root}  →  {prefix}")

    ignore_path = sync_config.resolve_ignore_path()
    ignore = IgnoreRules.load(ignore_path)
    excludes = sync_config.load_excludes(config_path)
    if excludes:
        info(f"Config excludes: {', '.join(excludes)}")
        ignore = ignore.with_extra_globs(excludes)
    if ignore._globs or ignore._regexes:
        info(
            f"Loaded {len(ignore._globs)} glob + {len(ignore._regexes)} regex "
            f"ignore rules (ignore file + config)"
        )

    active_sources = [(root, prefix) for root, prefix in sources if root.is_dir()]
    missing = [root for root, _ in sources if not root.is_dir()]
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
