#!/usr/bin/env python3
"""Full status dashboard process for macOS (controls via Unix socket IPC)."""

from __future__ import annotations

import logging
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import scrolledtext

from supervisor.icon import apply_tk_icon
from supervisor.ipc import request
from supervisor.paths import LOGS_DIR
from supervisor.status_ipc import StatusRaiseServer, raise_existing
from supervisor.ui_theme import BG as _BG
from supervisor.ui_theme import BTN_BG as _BTN_BG
from supervisor.ui_theme import FG as _FG
from supervisor.ui_theme import GREEN as _GREEN
from supervisor.ui_theme import RED as _RED
from supervisor.ui_theme import configure_dark_style, make_button

MAX_LOG_CHARS = 60_000

LOGS_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    filename=LOGS_DIR / "status_viewer.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    encoding="utf-8",
)
_log = logging.getLogger("mediactl.status")


class StatusDashboard:
    def __init__(self) -> None:
        _log.info("Status viewer starting")
        self._raise_server = StatusRaiseServer(self._raise_to_front)
        if not self._raise_server.start():
            _log.info("Another status viewer is already open — raising it")
            return
        self._root = tk.Tk()
        self._root.title("mediactl")
        self._root.geometry("920x640")
        self._root.configure(bg=_BG)
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)
        apply_tk_icon(self._root)
        self._btn_style = configure_dark_style(self._root)

        mono = tkfont.Font(family="Menlo", size=9)
        body = tkfont.Font(size=10)

        self._main = tk.Frame(self._root, bg=_BG)
        self._main.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self._dot_labels: dict[str, tk.Label] = {}
        self._status_labels: dict[str, tk.Label] = {}
        self._log_widgets: dict[str, scrolledtext.ScrolledText] = {}
        self._log_cache: dict[str, str] = {}
        self._follow_tail: dict[str, bool] = {}  # auto-scroll only while True
        self._sections_built = False
        self._body_font = body
        self._mono_font = mono

        footer = tk.Frame(self._root, bg=_BG)
        footer.pack(fill=tk.X, padx=8, pady=(0, 8))

        self._autostart_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            footer,
            text="Launch at startup",
            variable=self._autostart_var,
            command=self._toggle_autostart,
            bg=_BG,
            fg=_FG,
            selectcolor=_BTN_BG,
            activebackground=_BG,
            activeforeground=_FG,
            font=body,
        ).pack(side=tk.LEFT)

        make_button(
            footer,
            "Quit mediactl",
            self._quit_mediactl,
            style_name=self._btn_style,
            side=tk.RIGHT,
        )

        self._error = tk.Label(self._root, text="", bg=_BG, fg=_RED, anchor=tk.W)
        self._error.pack(fill=tk.X, padx=8)

        self._poll()
        try:
            self._root.mainloop()
        finally:
            self._raise_server.stop()

    def _on_close(self) -> None:
        self._raise_server.stop()
        self._root.destroy()

    def _raise_to_front(self) -> None:
        def _do() -> None:
            try:
                self._root.deiconify()
                self._root.lift()
                self._root.attributes("-topmost", True)
                self._root.after(200, lambda: self._root.attributes("-topmost", False))
                self._root.focus_force()
            except tk.TclError:
                pass

        try:
            self._root.after(0, _do)
        except Exception:
            pass

    def _ensure_sections(self, workers: list[dict]) -> None:
        if self._sections_built:
            return
        for w in workers:
            self._build_worker_section(w["name"])
        self._sections_built = True

    def _build_worker_section(self, name: str) -> None:
        section = tk.Frame(self._main, bg=_BG)
        section.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        header = tk.Frame(section, bg=_BG)
        header.pack(fill=tk.X)

        dot = tk.Label(header, text="●", font=self._body_font, bg=_BG, fg=_RED)
        dot.pack(side=tk.LEFT, padx=(0, 6))
        self._dot_labels[name] = dot

        tk.Label(
            header,
            text=name,
            font=self._body_font,
            bg=_BG,
            fg=_FG,
            anchor=tk.W,
        ).pack(side=tk.LEFT)

        status = tk.Label(header, text="", font=self._body_font, bg=_BG, fg=_FG, anchor=tk.W)
        status.pack(side=tk.LEFT, padx=(12, 0))
        self._status_labels[name] = status

        btn_row = tk.Frame(section, bg=_BG)
        btn_row.pack(fill=tk.X, pady=(4, 4))
        for label, cmd in (("Start", "start"), ("Stop", "stop"), ("Restart", "restart")):
            make_button(
                btn_row,
                label,
                lambda c=cmd, n=name: self._worker_cmd(c, n),
                style_name=self._btn_style,
                side=tk.LEFT,
                padx=(0, 4),
            )

        txt = scrolledtext.ScrolledText(
            section,
            height=8,
            font=self._mono_font,
            bg=_BG,
            fg=_FG,
            insertbackground=_FG,
            state=tk.DISABLED,
            wrap=tk.NONE,
        )
        txt.pack(fill=tk.BOTH, expand=True)
        self._log_widgets[name] = txt
        self._follow_tail[name] = True
        self._bind_log_scroll(name, txt)

        make_button(
            section,
            "Follow latest",
            lambda n=name: self._follow_latest(n),
            style_name=self._btn_style,
            anchor=tk.W,
            pady=(4, 0),
        )

    def _worker_cmd(self, cmd: str, name: str) -> None:
        def run() -> None:
            try:
                _log.info("UI %s worker=%s", cmd, name)
                resp = request({"cmd": cmd, "worker": name})
                _log.info("UI %s response: %s", cmd, resp)
            except Exception as exc:
                _log.exception("UI %s failed", cmd)
                self._root.after(0, lambda: self._error.configure(text=str(exc)))

        threading.Thread(target=run, daemon=True).start()

    def _toggle_autostart(self) -> None:
        enabled = bool(self._autostart_var.get())

        def run() -> None:
            try:
                _log.info("UI set_autostart enabled=%s", enabled)
                resp = request({"cmd": "set_autostart", "enabled": enabled})
                _log.info("UI set_autostart response: %s", resp)
            except Exception as exc:
                _log.exception("UI set_autostart failed")
                self._root.after(0, lambda: self._error.configure(text=str(exc)))

        threading.Thread(target=run, daemon=True).start()

    def _quit_mediactl(self) -> None:
        def run() -> None:
            try:
                _log.info("UI quit mediactl")
                request({"cmd": "quit"})
            except Exception as exc:
                _log.warning("UI quit request error: %s", exc)
            self._root.after(0, self._root.destroy)

        threading.Thread(target=run, daemon=True).start()

    def _poll(self) -> None:
        try:
            resp = request({"cmd": "status"}, timeout=1.5)
            if not resp.get("ok", True) and "workers" not in resp:
                raise RuntimeError(resp.get("error", "status failed"))
            workers = resp.get("workers") or []
            self._ensure_sections(workers)
            self._autostart_var.set(bool(resp.get("autostart")))
            for w in workers:
                name = w["name"]
                dot = self._dot_labels.get(name)
                label = self._status_labels.get(name)
                if not dot or not label:
                    continue
                if w.get("running"):
                    dot.configure(fg=_GREEN)
                    label.configure(
                        text=f"running  pid {w.get('pid') or '?'}  uptime {w.get('uptime') or '—'}"
                    )
                else:
                    dot.configure(fg=_RED)
                    label.configure(text="stopped")
                self._refresh_log(name)
            self._error.configure(text="")
        except Exception as exc:
            # Log disconnects at most once per contiguous failure stretch via cache key
            if self._log_cache.get("__conn_err__") != str(exc):
                self._log_cache["__conn_err__"] = str(exc)
                _log.warning("Not connected to mediactl: %s", exc)
            self._error.configure(text=f"Not connected to mediactl: {exc}")
        else:
            if "__conn_err__" in self._log_cache:
                _log.info("Reconnected to mediactl control socket")
                del self._log_cache["__conn_err__"]
        self._root.after(500, self._poll)

    def _follow_latest(self, name: str) -> None:
        self._follow_tail[name] = True
        # Force a refresh so the newest log content is loaded, then pin to end
        self._log_cache.pop(name, None)
        self._refresh_log(name)
        txt = self._log_widgets.get(name)
        if txt is not None:
            txt.see(tk.END)
        _log.info("UI follow latest worker=%s", name)

    def _bind_log_scroll(self, name: str, txt: scrolledtext.ScrolledText) -> None:
        """Pause follow-tail when the user scrolls away from the bottom."""

        def on_scroll(*_args) -> None:
            self._follow_tail[name] = self._is_at_bottom(txt)

        txt.vbar.configure(command=lambda *a: (txt.yview(*a), on_scroll()))
        txt.configure(yscrollcommand=lambda *a: (txt.vbar.set(*a), on_scroll()))
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>", "<ButtonRelease-1>"):
            txt.bind(seq, lambda _e: self._root.after_idle(on_scroll), add="+")

    @staticmethod
    def _is_at_bottom(txt: scrolledtext.ScrolledText, slack: float = 0.02) -> bool:
        try:
            _top, bottom = txt.yview()
        except tk.TclError:
            return True
        return bottom >= 1.0 - slack

    def _refresh_log(self, worker_name: str) -> None:
        txt = self._log_widgets.get(worker_name)
        if not txt:
            return
        slug = worker_name.lower().replace(" ", "_")
        path = LOGS_DIR / f"{slug}.log"
        content = ""
        if path.is_file():
            try:
                content = path.read_text(encoding="utf-8", errors="replace")[-MAX_LOG_CHARS:]
            except OSError:
                content = f"(could not read {path})"
        if self._log_cache.get(worker_name) == content:
            return
        follow = self._follow_tail.get(worker_name, True)
        yview = txt.yview() if not follow else None
        self._log_cache[worker_name] = content
        txt.configure(state=tk.NORMAL)
        txt.delete("1.0", tk.END)
        txt.insert(tk.END, content)
        if follow:
            txt.see(tk.END)
        elif yview is not None:
            txt.yview_moveto(yview[0])
        txt.configure(state=tk.DISABLED)


def main() -> None:
    if raise_existing():
        _log.info("Raised existing status viewer")
        return
    StatusDashboard()


if __name__ == "__main__":
    main()
