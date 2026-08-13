"""Managed subprocess worker with log tail and NOTIFY parsing."""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from supervisor.log_coalesce import LogCoalescer, LogEntry, LogMode, ProgressFileThrottle
from supervisor.paths import LOGS_DIR, SRC_DIR

_log = logging.getLogger("mediactl")


def worker_python() -> str:
    """Workers need console python so stdout pipes work (not pythonw)."""
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        return exe.replace("pythonw.exe", "python.exe")
    return exe


class WorkerProcess:
    """
    Manages a single subprocess. Reads stdout line-by-line in a daemon thread,
    puts lines into `log_queue`, fires callbacks for NOTIFY: lines.
    """

    def __init__(self, config: dict, log_queue: queue.Queue[LogEntry], notify_cb) -> None:
        self.name: str = config["name"]
        self._cmd: list[str] = config["cmd"]
        self._cwd: Path = config["cwd"]
        self._env_extra: dict[str, str] = config.get("env") or {}
        self._notifications: list[dict] = config["notifications"]
        self._progress_patterns: list[str] = list(config.get("progress_patterns", []))
        self._coalescer = LogCoalescer(self._progress_patterns)
        self._file_throttle = ProgressFileThrottle()
        self._log_queue = log_queue
        self._notify_cb = notify_cb
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._stopped = False
        self._start_time: float | None = None
        LOGS_DIR.mkdir(exist_ok=True)
        slug = self.name.lower().replace(" ", "_")
        self._log_path = LOGS_DIR / f"{slug}.log"
        self._log_fh: object | None = None

    def start(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return
            self._stopped = False
            _log.info("Starting worker: %s  cmd=%s", self.name, self._cmd)
            path = os.environ.get("PATH", "")
            extras = "/opt/homebrew/bin:/usr/local/bin"
            if extras not in path:
                path = f"{extras}:{path}" if path else extras
            env = {
                **os.environ,
                "PATH": path,
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
                "PYTHONPATH": str(SRC_DIR),
                **self._env_extra,
            }
            _log.info(
                "Worker %s env PATH(head)=%s PYTHONPATH=%s cwd=%s log=%s",
                self.name,
                path[:120],
                env.get("PYTHONPATH"),
                self._cwd,
                self._log_path,
            )
            popen_kwargs: dict = {
                "cwd": self._cwd,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "bufsize": 1,
                "env": env,
            }
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            self._proc = subprocess.Popen(self._cmd, **popen_kwargs)
            self._start_time = time.monotonic()
            self._log(f"[mediactl] started (pid {self._proc.pid})")
            _log.info("Worker %s started (pid %d)", self.name, self._proc.pid)
            threading.Thread(target=self._read_loop, daemon=True).start()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
                self._log("[mediactl] stopped")
                _log.info("Worker %s stopped", self.name)
            self._start_time = None

    def restart(self) -> None:
        self.stop()
        time.sleep(0.5)
        self.start()

    @property
    def running(self) -> bool:
        with self._lock:
            return bool(self._proc and self._proc.poll() is None)

    @property
    def pid(self) -> int | None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return self._proc.pid
            return None

    @property
    def uptime(self) -> str:
        with self._lock:
            running = bool(self._proc and self._proc.poll() is None)
            if not running or self._start_time is None:
                return "—"
            elapsed = int(time.monotonic() - self._start_time)
        return f"{elapsed // 3600:02d}:{elapsed % 3600 // 60:02d}:{elapsed % 60:02d}"

    @property
    def log_path(self) -> Path:
        return self._log_path

    @property
    def progress_patterns(self) -> list[str]:
        return self._progress_patterns

    def _log(self, line: str, mode: LogMode = "append", *, is_progress: bool = False) -> None:
        self._log_queue.put((self.name, line, mode))
        for file_line in self._file_lines(line, mode, is_progress):
            self._write_file_line(file_line)

    def _file_lines(self, line: str, mode: LogMode, is_progress: bool) -> list[str]:
        if not is_progress:
            flushed = self._file_throttle.flush_before_normal()
            return [flushed, line] if flushed else [line]
        if mode == "append":
            return [self._file_throttle.note_append_progress(line)]
        disk_line = self._file_throttle.note_replace(line)
        return [disk_line] if disk_line else []

    def _write_file_line(self, line: str) -> None:
        try:
            if self._log_fh is None:
                self._log_fh = open(self._log_path, "a", encoding="utf-8", buffering=1)
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            self._log_fh.write(f"{ts}  {line}\n")  # type: ignore[union-attr]
        except Exception:
            pass

    def _read_loop(self) -> None:
        proc = self._proc
        assert proc and proc.stdout
        try:
            for raw in proc.stdout:
                line = raw.rstrip()
                self._dispatch(line)
                mode, is_progress = self._coalescer.process(line)
                self._log(line, mode, is_progress=is_progress)
        except Exception as exc:
            _log.warning("Worker %s read error: %s", self.name, exc)
        finally:
            proc.wait()
            if not self._stopped:
                self._log(
                    f"[mediactl] process exited (code {proc.returncode}) — restarting in 5s"
                )
                _log.warning(
                    "Worker %s exited (code %d), restarting in 5s",
                    self.name,
                    proc.returncode,
                )
                time.sleep(5)
                self.start()

    def _dispatch(self, line: str) -> None:
        for spec in self._notifications:
            if line.startswith(spec["prefix"] + "|"):
                body = line[len(spec["prefix"]) + 1 :]
                self._notify_cb(spec["title"], body)
                return
            if line == spec["prefix"]:
                self._notify_cb(spec["title"], "")
                return
