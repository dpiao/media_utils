#!/usr/bin/env python3
"""
mediactl.py — System-tray supervisor for background media scripts.

Manages render_vr360.py and sync_media_to_s3.py as persistent subprocesses,
parses NOTIFY: lines from their stdout for Windows toast notifications, and
shows a per-worker log window via Tkinter.

Usage:
    pythonw mediactl.py          (silent background, tray icon only)
    python   mediactl.py         (console visible, useful for debugging)

Auto-start: right-click tray → "Launch at startup" to toggle registry key.
"""

import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import winreg
from pathlib import Path
from tkinter import font as tkfont  # noqa: F401
from tkinter import scrolledtext, ttk

import pystray
from PIL import Image, ImageDraw

# ── File logging (always on, so crashes are diagnosable) ──────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_FILE  = REPO_ROOT / "mediactl.log"
LOGS_DIR  = REPO_ROOT / "logs"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    encoding="utf-8",
)
logging.getLogger("PIL").setLevel(logging.WARNING)
_log = logging.getLogger("mediactl")

def _log_uncaught(exc_type, exc_value, exc_tb):
    _log.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
    sys.__excepthook__(exc_type, exc_value, exc_tb)

sys.excepthook = _log_uncaught

# ── Worker definitions ────────────────────────────────────────────────────────

THIS_DIR = Path(__file__).resolve().parent

# Workers need python.exe (not pythonw.exe) so their stdout/stderr pipes work.
# We hide the console via CREATE_NO_WINDOW instead.
_exe = sys.executable
PYTHON = _exe.replace("pythonw.exe", "python.exe") if _exe.lower().endswith("pythonw.exe") else _exe

WORKERS: list[dict] = [
    {
        "name": "Render VR360",
        "cmd": [PYTHON, str(THIS_DIR / "render_vr360.py")],
        "cwd": THIS_DIR,
        "notifications": [
            {"prefix": "NOTIFY:Render started",   "title": "Render started"},
            {"prefix": "NOTIFY:Render complete",  "title": "Render complete"},
            {"prefix": "NOTIFY:Render failed",    "title": "Render failed"},
        ],
    },
    {
        "name": "S3 Sync",
        "cmd": [PYTHON, str(THIS_DIR / "sync_media_to_s3.py"), "--no-initial-sync"],
        "cwd": THIS_DIR,
        "notifications": [
            {"prefix": "NOTIFY:Upload pending",    "title": "S3 upload pending"},
            {"prefix": "NOTIFY:Uploaded to S3",   "title": "S3 upload complete"},
            {"prefix": "NOTIFY:Warning",          "title": "S3 Sync warning"},
        ],
    },
]

# ── Autostart registry ────────────────────────────────────────────────────────

APP_NAME = "mediactl"
RUN_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
# Use pythonw so no console window appears at startup
PYTHONW  = Path(PYTHON).with_name("pythonw.exe")
AUTOSTART_CMD = f'"{PYTHONW}" "{Path(__file__).resolve()}"'


def is_autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except FileNotFoundError:
        return False


def set_autostart(enable: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, access=winreg.KEY_SET_VALUE) as key:
        if enable:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, AUTOSTART_CMD)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


# ── Worker process ────────────────────────────────────────────────────────────

class WorkerProcess:
    """
    Manages a single subprocess. Reads stdout line-by-line in a daemon thread,
    puts lines into `log_queue`, fires callbacks for NOTIFY: lines.
    """

    def __init__(self, config: dict, log_queue: "queue.Queue[tuple[str,str]]", notify_cb) -> None:
        self.name: str = config["name"]
        self._cmd: list[str] = config["cmd"]
        self._cwd: Path = config["cwd"]
        self._notifications: list[dict] = config["notifications"]
        self._log_queue = log_queue
        self._notify_cb = notify_cb
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._stopped = False
        # Per-worker log file: logs/<slug>.log
        LOGS_DIR.mkdir(exist_ok=True)
        slug = self.name.lower().replace(" ", "_")
        self._log_path = LOGS_DIR / f"{slug}.log"
        self._log_fh: "None | object" = None  # opened lazily on first write

    # ── public API ──────────────────────────────────────────────────────────

    def start(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return
            self._stopped = False
            _log.info("Starting worker: %s  cmd=%s", self.name, self._cmd)
            env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
            self._proc = subprocess.Popen(
                self._cmd,
                cwd=self._cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW,
                env=env,
            )
            self._log(f"[mediactl] started (pid {self._proc.pid})")
            _log.info("Worker %s started (pid %d)", self.name, self._proc.pid)
            t = threading.Thread(target=self._read_loop, daemon=True)
            t.start()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
                self._log("[mediactl] stopped")
                _log.info("Worker %s stopped", self.name)

    def restart(self) -> None:
        self.stop()
        time.sleep(0.5)
        self.start()

    @property
    def running(self) -> bool:
        with self._lock:
            return bool(self._proc and self._proc.poll() is None)

    # ── internals ───────────────────────────────────────────────────────────

    @property
    def log_path(self) -> Path:
        return self._log_path

    def _log(self, line: str) -> None:
        self._log_queue.put((self.name, line))
        # Also append to the per-worker log file
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
                self._log(line)
        except Exception as exc:
            _log.warning("Worker %s read error: %s", self.name, exc)
        finally:
            proc.wait()
            if not self._stopped:
                self._log(f"[mediactl] process exited (code {proc.returncode}) — restarting in 5s")
                _log.warning("Worker %s exited (code %d), restarting in 5s", self.name, proc.returncode)
                time.sleep(5)
                self.start()

    def _dispatch(self, line: str) -> None:
        for spec in self._notifications:
            if line.startswith(spec["prefix"] + "|"):
                body = line[len(spec["prefix"]) + 1:]
                self._notify_cb(spec["title"], body)
                return
            if line == spec["prefix"]:
                self._notify_cb(spec["title"], "")
                return


# ── Log window (Tkinter) ──────────────────────────────────────────────────────

class LogWindow:
    MAX_LINES = 2000

    def __init__(self, workers: list[WorkerProcess], log_queue: "queue.Queue[tuple[str,str]]") -> None:
        self._workers = workers
        self._log_queue = log_queue
        self._root: tk.Tk | None = None
        self._widgets: dict[str, scrolledtext.ScrolledText] = {}
        self._line_counts: dict[str, int] = {}

    def show(self) -> None:
        if self._root and self._root.winfo_exists():
            self._root.deiconify()
            self._root.lift()
            return
        self._build()

    def _build(self) -> None:
        root = tk.Tk()
        self._root = root
        root.title("mediactl — logs")
        root.geometry("900x550")
        root.configure(bg="#1e1e1e")
        root.protocol("WM_DELETE_WINDOW", root.withdraw)

        notebook = ttk.Notebook(root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        mono = tkfont.Font(family="Consolas", size=9)

        for worker in self._workers:
            frame = ttk.Frame(notebook)
            notebook.add(frame, text=worker.name)

            txt = scrolledtext.ScrolledText(
                frame,
                font=mono,
                bg="#1e1e1e",
                fg="#d4d4d4",
                insertbackground="#d4d4d4",
                state=tk.DISABLED,
                wrap=tk.NONE,
            )
            txt.pack(fill=tk.BOTH, expand=True)
            self._widgets[worker.name] = txt
            self._line_counts[worker.name] = 0

        btn_frame = tk.Frame(root, bg="#1e1e1e")
        btn_frame.pack(fill=tk.X, padx=4, pady=2)
        for worker in self._workers:
            w = worker
            tk.Button(
                btn_frame, text=f"Restart {w.name}",
                command=lambda ww=w: threading.Thread(target=ww.restart, daemon=True).start(),
                bg="#3c3c3c", fg="#d4d4d4", relief=tk.FLAT, padx=8,
            ).pack(side=tk.LEFT, padx=4)

        self._poll()
        root.mainloop()

    def _poll(self) -> None:
        if not self._root:
            return
        try:
            while True:
                name, line = self._log_queue.get_nowait()
                self._append(name, line)
        except queue.Empty:
            pass
        self._root.after(200, self._poll)

    def _append(self, name: str, line: str) -> None:
        txt = self._widgets.get(name)
        if not txt:
            return
        txt.configure(state=tk.NORMAL)
        txt.insert(tk.END, line + "\n")
        self._line_counts[name] = self._line_counts.get(name, 0) + 1
        if self._line_counts[name] > self.MAX_LINES:
            txt.delete("1.0", "500.0")
            self._line_counts[name] = max(0, self._line_counts[name] - 500)
        txt.see(tk.END)
        txt.configure(state=tk.DISABLED)


# ── Tray icon ─────────────────────────────────────────────────────────────────

def _make_icon_image(color: str = "#4a9eff") -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([4, 4, size - 4, size - 4], fill=color)
    return img


class TrayApp:
    def __init__(self) -> None:
        self._log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._workers = [WorkerProcess(cfg, self._log_queue, self._on_notify) for cfg in WORKERS]
        self._log_window = LogWindow(self._workers, self._log_queue)
        self._icon: pystray.Icon | None = None
        self._notify_queue: queue.Queue[tuple[str, str]] = queue.Queue()

    # ── startup ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        _log.info("mediactl starting, log file: %s", LOG_FILE)
        for w in self._workers:
            w.start()

        threading.Thread(target=self._notify_loop, daemon=True).start()

        menu = pystray.Menu(
            pystray.MenuItem("Show logs", self._show_logs, default=True),
            pystray.Menu.SEPARATOR,
            *[self._worker_submenu(w) for w in self._workers],
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Launch at startup",
                self._toggle_autostart,
                checked=lambda item: is_autostart_enabled(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )

        self._icon = pystray.Icon(
            APP_NAME,
            _make_icon_image(),
            "mediactl",
            menu=menu,
        )
        self._icon.run(setup=self._on_icon_ready)

    # ── tray callbacks ───────────────────────────────────────────────────────

    def _on_icon_ready(self, icon: pystray.Icon) -> None:
        icon.visible = True
        _log.info("Tray icon ready and visible")

    def _show_logs(self, icon=None, item=None) -> None:
        threading.Thread(target=self._log_window.show, daemon=True).start()

    @staticmethod
    def _run_in_thread(fn):
        """Return a pystray-compatible action (icon, item) that runs fn in a thread."""
        def _action(icon, item):
            threading.Thread(target=fn, daemon=True).start()
        return _action

    def _worker_submenu(self, w: WorkerProcess) -> pystray.MenuItem:
        return pystray.MenuItem(
            w.name,
            pystray.Menu(
                pystray.MenuItem("Stop",    self._run_in_thread(w.stop)),
                pystray.MenuItem("Start",   self._run_in_thread(w.start)),
                pystray.MenuItem("Restart", self._run_in_thread(w.restart)),
            ),
        )

    def _toggle_autostart(self, icon, item) -> None:
        set_autostart(not is_autostart_enabled())

    def _quit(self, icon, item) -> None:
        _log.info("Quit requested")
        for w in self._workers:
            w.stop()
        icon.stop()

    # ── notifications ────────────────────────────────────────────────────────

    def _on_notify(self, title: str, body: str) -> None:
        self._notify_queue.put((title, body))

    def _notify_loop(self) -> None:
        """Deliver notifications on a background thread to avoid blocking workers."""
        while True:
            title, body = self._notify_queue.get()
            _log.info("NOTIFY %s | %s", title, body)
            if self._icon:
                try:
                    msg = body if body else title
                    self._icon.notify(msg, title)
                except Exception as exc:
                    _log.warning("Toast failed: %s", exc)


# ── Entry point ───────────────────────────────────────────────────────────────

def _acquire_single_instance_mutex() -> None:
    """Exit immediately if another mediactl process is already running."""
    import ctypes
    _MUTEX_NAME = "Global\\mediactl_singleton"
    ctypes.windll.kernel32.CreateMutexW(None, True, _MUTEX_NAME)
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        _log.warning("Another mediactl instance is already running — exiting.")
        sys.exit(0)


def main() -> None:
    _acquire_single_instance_mutex()
    TrayApp().run()


if __name__ == "__main__":
    main()
