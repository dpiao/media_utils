"""Dark-theme Tk helpers. macOS Aqua ignores tk.Button colors — use ttk clam."""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk

_BG = "#1e1e1e"
_FG = "#d4d4d4"
_BTN_BG = "#3c3c3c"
_BTN_ACTIVE = "#505050"
_BTN_BORDER = "#555555"


def configure_dark_style(root: tk.Misc) -> str:
    """Return ttk style name for dark buttons; no-op theme setup on Windows ok too."""
    style = ttk.Style(root)
    # clam honors background/foreground; aqua/default on macOS does not
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    name = "Mediactl.TButton"
    style.configure(
        name,
        background=_BTN_BG,
        foreground=_FG,
        bordercolor=_BTN_BORDER,
        darkcolor=_BTN_BG,
        lightcolor=_BTN_BG,
        focuscolor=_BTN_BG,
        relief="flat",
        padding=(10, 4),
    )
    style.map(
        name,
        background=[("active", _BTN_ACTIVE), ("pressed", "#2a2a2a")],
        foreground=[("disabled", "#777777"), ("active", "#ffffff")],
    )
    return name


def make_button(parent: tk.Misc, text: str, command, *, style_name: str, **pack_kwargs):
    btn = ttk.Button(parent, text=text, command=command, style=style_name)
    if pack_kwargs:
        btn.pack(**pack_kwargs)
    return btn


# Re-export palette for screens
BG, FG, BTN_BG = _BG, _FG, _BTN_BG
GREEN = "#4caf50"
RED = "#f44336"
IS_MAC = sys.platform == "darwin"
