"""Tkinter status dashboard for mediactl."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import scrolledtext

from supervisor import platform as plat
from supervisor.icon import apply_tk_icon
from supervisor.worker import WorkerProcess

_BG = "#1e1e1e"
_FG = "#d4d4d4"
_BTN_BG = "#3c3c3c"
_GREEN = "#4caf50"
_RED = "#f44336"


class StatusWindow:
    MAX_LINES = 500

    def __init__(
        self,
        workers: list[WorkerProcess],
        log_queue: queue.Queue[tuple[str, str]],
        on_quit,
        on_autostart_toggle,
    ) -> None:
        self._workers = workers
        self._log_queue = log_queue
        self._on_quit = on_quit
        self._on_autostart_toggle = on_autostart_toggle
        self._root: tk.Tk | None = None
        self._log_widgets: dict[str, scrolledtext.ScrolledText] = {}
        self._line_counts: dict[str, int] = {}
        self._status_labels: dict[str, tk.Label] = {}
        self._dot_labels: dict[str, tk.Label] = {}
        self._autostart_var: tk.BooleanVar | None = None

    def show(self) -> None:
        if self._root and self._root.winfo_exists():
            self._root.deiconify()
            self._root.lift()
            return
        self._build()

    def _build(self) -> None:
        root = tk.Tk()
        self._root = root
        root.title("mediactl")
        root.geometry("920x640")
        root.configure(bg=_BG)
        root.protocol("WM_DELETE_WINDOW", root.withdraw)
        apply_tk_icon(root)

        mono = tkfont.Font(family="Menlo" if __import__("sys").platform == "darwin" else "Consolas", size=9)
        body = tkfont.Font(size=10)

        main = tk.Frame(root, bg=_BG)
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        for worker in self._workers:
            self._build_worker_section(main, worker, mono, body)

        footer = tk.Frame(root, bg=_BG)
        footer.pack(fill=tk.X, padx=8, pady=(0, 8))

        self._autostart_var = tk.BooleanVar(value=plat.is_autostart_enabled())
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

        tk.Button(
            footer,
            text="Quit",
            command=self._on_quit,
            bg=_BTN_BG,
            fg=_FG,
            relief=tk.FLAT,
            padx=12,
            font=body,
        ).pack(side=tk.RIGHT)

        self._poll()
        root.mainloop()

    def _build_worker_section(
        self,
        parent: tk.Frame,
        worker: WorkerProcess,
        mono: tkfont.Font,
        body: tkfont.Font,
    ) -> None:
        section = tk.Frame(parent, bg=_BG)
        section.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        header = tk.Frame(section, bg=_BG)
        header.pack(fill=tk.X)

        dot = tk.Label(header, text="●", font=body, bg=_BG, fg=_RED)
        dot.pack(side=tk.LEFT, padx=(0, 6))
        self._dot_labels[worker.name] = dot

        tk.Label(
            header,
            text=worker.name,
            font=body,
            bg=_BG,
            fg=_FG,
            anchor=tk.W,
        ).pack(side=tk.LEFT)

        status = tk.Label(header, text="", font=body, bg=_BG, fg=_FG, anchor=tk.W)
        status.pack(side=tk.LEFT, padx=(12, 0))
        self._status_labels[worker.name] = status

        btn_row = tk.Frame(section, bg=_BG)
        btn_row.pack(fill=tk.X, pady=(4, 4))
        for label, fn in (
            ("Start", worker.start),
            ("Stop", worker.stop),
            ("Restart", worker.restart),
        ):
            tk.Button(
                btn_row,
                text=label,
                command=lambda f=fn: threading.Thread(target=f, daemon=True).start(),
                bg=_BTN_BG,
                fg=_FG,
                relief=tk.FLAT,
                padx=8,
                font=body,
            ).pack(side=tk.LEFT, padx=(0, 4))

        txt = scrolledtext.ScrolledText(
            section,
            height=8,
            font=mono,
            bg=_BG,
            fg=_FG,
            insertbackground=_FG,
            state=tk.DISABLED,
            wrap=tk.NONE,
        )
        txt.pack(fill=tk.BOTH, expand=True)
        self._log_widgets[worker.name] = txt
        self._line_counts[worker.name] = 0

    def _toggle_autostart(self) -> None:
        enabled = bool(self._autostart_var and self._autostart_var.get())
        plat.set_autostart(enabled)
        if self._on_autostart_toggle:
            self._on_autostart_toggle(enabled)

    def _poll(self) -> None:
        if not self._root:
            return
        try:
            while True:
                name, line = self._log_queue.get_nowait()
                self._append(name, line)
        except queue.Empty:
            pass
        self._refresh_status()
        self._root.after(500, self._poll)

    def _refresh_status(self) -> None:
        for worker in self._workers:
            dot = self._dot_labels.get(worker.name)
            label = self._status_labels.get(worker.name)
            if not dot or not label:
                continue
            if worker.running:
                dot.configure(fg=_GREEN)
                pid = worker.pid or "?"
                label.configure(text=f"running  pid {pid}  uptime {worker.uptime}")
            else:
                dot.configure(fg=_RED)
                label.configure(text="stopped")

    def _append(self, name: str, line: str) -> None:
        txt = self._log_widgets.get(name)
        if not txt:
            return
        txt.configure(state=tk.NORMAL)
        txt.insert(tk.END, line + "\n")
        self._line_counts[name] = self._line_counts.get(name, 0) + 1
        if self._line_counts[name] > self.MAX_LINES:
            txt.delete("1.0", "100.0")
            self._line_counts[name] = max(0, self._line_counts[name] - 100)
        txt.see(tk.END)
        txt.configure(state=tk.DISABLED)
