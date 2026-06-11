#!/usr/bin/env python3
"""
sync_movies_s3.py — Watch local movie folders and sync new/changed files to S3.

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

Dependencies: watchdog (pip install watchdog), AWS CLI (aws configure)
"""

import argparse
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
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        warn(f"aws exited {result.returncode}: {result.stderr.strip()}")
    else:
        ok(" ".join(cmd[:4]) + " ...")


def upload_file(
    local_root: Path,
    file_path: Path,
    s3_prefix: str,
    dry_run: bool,
) -> None:
    dest = s3_key(local_root, file_path, s3_prefix)
    run_aws(["aws", "s3", "cp", str(file_path), dest], dry_run)


# ── Stability checker ─────────────────────────────────────────────────────────

class StabilityChecker:
    """
    Deduplicates file events and uploads each file only after its size has
    been stable for `stable_secs` consecutive seconds.
    """

    def __init__(self, stable_secs: int, dry_run: bool) -> None:
        self._stable_secs = stable_secs
        self._dry_run = dry_run
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
                upload_file(root, path, prefix, self._dry_run)


# ── Watchdog handler ──────────────────────────────────────────────────────────

class MovieHandler(FileSystemEventHandler):
    def __init__(
        self,
        local_root: Path,
        s3_prefix: str,
        checker: StabilityChecker,
    ) -> None:
        self._root = local_root
        self._prefix = s3_prefix
        self._checker = checker

    def _handle(self, path_str: str) -> None:
        path = Path(path_str)
        if not path.is_file():
            return
        if is_temp(path):
            return
        self._checker.enqueue(path, self._root, self._prefix)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(event.dest_path)


# ── Initial sync ──────────────────────────────────────────────────────────────

def initial_sync(local_root: Path, s3_prefix: str, dry_run: bool) -> None:
    step(f"Initial sync: {local_root}  →  {s3_prefix}")
    exclude_args: list[str] = []
    for ext in sorted(TEMP_EXTENSIONS):
        exclude_args += ["--exclude", f"*{ext}"]
    run_aws(
        ["aws", "s3", "sync", str(local_root), s3_prefix] + exclude_args,
        dry_run,
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Watch movie folders and sync new/changed files to S3."
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

    active_sources = [(root, prefix) for root, prefix in SOURCES if root.is_dir()]
    missing = [root for root, _ in SOURCES if not root.is_dir()]
    for m in missing:
        warn(f"Source folder not found, skipping: {m}")
    if not active_sources:
        warn("No source folders available. Exiting.")
        sys.exit(1)

    if not args.no_initial_sync:
        for root, prefix in active_sources:
            initial_sync(root, prefix, args.dry_run)

    checker = StabilityChecker(args.stable_secs, args.dry_run)
    observer = Observer()

    step("Starting file watcher")
    for root, prefix in active_sources:
        handler = MovieHandler(root, prefix, checker)
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
