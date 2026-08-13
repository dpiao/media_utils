#!/usr/bin/env python3
"""Tray supervisor (mediactl) — shared TrayApp entrypoint."""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading

import pystray

from supervisor import platform as plat
from supervisor.icon import load_icon
from supervisor.ipc import ControlServer
from supervisor.paths import APP_NAME, LOG_FILE, REPO_ROOT, SRC_DIR
from supervisor.status_ipc import raise_existing
from supervisor.log_coalesce import LogEntry
from supervisor.status_window import StatusWindow
from supervisor.worker import WorkerProcess, worker_python

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


class TrayApp:
    def __init__(self) -> None:
        self._log_queue: queue.Queue[LogEntry] = queue.Queue()
        workers_cfg = plat.get_workers()
        self._workers = [
            WorkerProcess(cfg, self._log_queue, self._on_notify) for cfg in workers_cfg
        ]
        self._workers_by_name = {w.name: w for w in self._workers}
        self._icon: pystray.Icon | None = None
        self._notify_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._control = ControlServer(self._handle_control)
        self._status_window = StatusWindow(
            self._workers,
            self._log_queue,
            on_quit=self._quit_from_gui,
            on_autostart_toggle=lambda _: None,
        )

    def run(self) -> None:
        _log.info("mediactl starting, log file: %s", LOG_FILE)
        plat.log_startup_diagnostics()
        self._control.start()
        for w in self._workers:
            w.start()

        threading.Thread(target=self._notify_loop, daemon=True).start()

        if sys.platform == "darwin":
            # Left-click opens status; menu is right-click only (see macos_tray)
            menu_items = [
                *[self._worker_submenu(w) for w in self._workers],
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "Launch at startup",
                    self._toggle_autostart,
                    checked=lambda item: plat.is_autostart_enabled(),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._quit),
            ]
        else:
            menu_items = [
                pystray.MenuItem("Show status", self._show_status, default=True),
                pystray.Menu.SEPARATOR,
                *[self._worker_submenu(w) for w in self._workers],
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "Launch at startup",
                    self._toggle_autostart,
                    checked=lambda item: plat.is_autostart_enabled(),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._quit),
            ]

        menu = pystray.Menu(*menu_items)

        self._icon = pystray.Icon(
            APP_NAME,
            load_icon(64),
            "mediactl",
            menu=menu,
        )
        self._icon.run(setup=self._on_icon_ready)

    def _on_icon_ready(self, icon: pystray.Icon) -> None:
        icon.visible = True
        if sys.platform == "darwin":
            from supervisor.macos_tray import install_left_click_status_right_click_menu

            install_left_click_status_right_click_menu(icon, self._show_status)
        _log.info("Tray icon ready and visible")

    def _show_status(self, icon=None, item=None) -> None:
        # Tk + pystray in one process crashes on macOS (Tk from a background
        # thread aborts the NSApp). Spawn a separate viewer process instead.
        if sys.platform == "darwin":
            self._show_status_macos()
            return
        threading.Thread(target=self._status_window.show, daemon=True).start()

    def _show_status_macos(self) -> None:
        if raise_existing():
            _log.info("Raised existing status viewer")
            return
        try:
            env = {
                **os.environ,
                "PYTHONPATH": str(SRC_DIR),
                "PYTHONUNBUFFERED": "1",
            }
            subprocess.Popen(
                [worker_python(), "-m", "supervisor.status_viewer"],
                cwd=str(REPO_ROOT),
                env=env,
                start_new_session=True,
            )
            _log.info("Opened status viewer process")
        except Exception as exc:
            _log.exception("Failed to open status viewer: %s", exc)

    def _quit_from_gui(self) -> None:
        if self._icon:
            self._quit(self._icon, None)

    @staticmethod
    def _run_in_thread(fn):
        def _action(icon, item):
            threading.Thread(target=fn, daemon=True).start()

        return _action

    def _worker_submenu(self, w: WorkerProcess) -> pystray.MenuItem:
        return pystray.MenuItem(
            w.name,
            pystray.Menu(
                pystray.MenuItem("Stop", self._run_in_thread(w.stop)),
                pystray.MenuItem("Start", self._run_in_thread(w.start)),
                pystray.MenuItem("Restart", self._run_in_thread(w.restart)),
            ),
        )

    def _toggle_autostart(self, icon, item) -> None:
        plat.set_autostart(not plat.is_autostart_enabled())

    def _quit(self, icon, item) -> None:
        _log.info("Quit requested")
        self._control.stop()
        for w in self._workers:
            w.stop()
        icon.stop()

    def _handle_control(self, req: dict) -> dict:
        cmd = req.get("cmd")
        # status is polled often — keep quiet unless debugging
        if cmd != "status":
            _log.info("Control request: %s", req)
        if cmd == "status":
            return {
                "ok": True,
                "autostart": plat.is_autostart_enabled(),
                "workers": [
                    {
                        "name": w.name,
                        "running": w.running,
                        "pid": w.pid,
                        "uptime": w.uptime,
                    }
                    for w in self._workers
                ],
            }
        if cmd in {"start", "stop", "restart"}:
            name = req.get("worker")
            worker = self._workers_by_name.get(name)
            if not worker:
                _log.warning("Control unknown worker: %s", name)
                return {"ok": False, "error": f"unknown worker: {name}"}
            fn = {"start": worker.start, "stop": worker.stop, "restart": worker.restart}[cmd]
            threading.Thread(target=fn, daemon=True).start()
            _log.info("Control %s queued for worker %s", cmd, name)
            return {"ok": True}
        if cmd == "set_autostart":
            enabled = bool(req.get("enabled"))
            plat.set_autostart(enabled)
            _log.info("Control set_autostart -> %s", enabled)
            return {"ok": True, "autostart": plat.is_autostart_enabled()}
        if cmd == "quit":
            _log.info("Control quit requested via status window")
            if self._icon:
                threading.Thread(
                    target=lambda: self._quit(self._icon, None),
                    daemon=True,
                ).start()
            return {"ok": True}
        _log.warning("Control unknown cmd: %s", cmd)
        return {"ok": False, "error": f"unknown cmd: {cmd}"}

    def _on_notify(self, title: str, body: str) -> None:
        self._notify_queue.put((title, body))

    def _notify_loop(self) -> None:
        while True:
            title, body = self._notify_queue.get()
            _log.info("NOTIFY %s | %s", title, body)
            if self._icon:
                try:
                    msg = body if body else title
                    self._icon.notify(msg, title)
                except Exception as exc:
                    _log.warning("Toast failed: %s", exc)


def main() -> None:
    plat.acquire_single_instance()
    plat.repair_autostart_if_needed()
    TrayApp().run()


if __name__ == "__main__":
    main()
