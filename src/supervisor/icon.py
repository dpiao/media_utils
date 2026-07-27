"""Load and prepare the mediactl app icon for tray and Tk windows."""

from __future__ import annotations

from functools import lru_cache

from PIL import Image, ImageDraw

from supervisor.paths import REPO_ROOT

ICON_PATH = REPO_ROOT / "assets" / "mediactl.png"


def _draw_fallback(size: int = 256) -> Image.Image:
    """Simple cloud + up-arrow if the PNG asset is missing."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    blue = (59, 158, 255, 255)
    s = float(size)
    d.ellipse([s * 0.18, s * 0.38, s * 0.52, s * 0.72], fill=blue)
    d.ellipse([s * 0.38, s * 0.28, s * 0.78, s * 0.68], fill=blue)
    d.ellipse([s * 0.55, s * 0.42, s * 0.88, s * 0.74], fill=blue)
    d.rounded_rectangle(
        [s * 0.22, s * 0.48, s * 0.82, s * 0.78],
        radius=int(s * 0.08),
        fill=blue,
    )
    # cut out up-arrow
    cx, cy = s * 0.5, s * 0.52
    arrow = [
        (cx, cy - s * 0.16),
        (cx + s * 0.14, cy + s * 0.02),
        (cx + s * 0.06, cy + s * 0.02),
        (cx + s * 0.06, cy + s * 0.16),
        (cx - s * 0.06, cy + s * 0.16),
        (cx - s * 0.06, cy + s * 0.02),
        (cx - s * 0.14, cy + s * 0.02),
    ]
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).polygon(arrow, fill=255)
    img.putalpha(
        Image.composite(
            Image.new("L", (size, size), 0),
            img.split()[3],
            mask,
        ),
    )
    return img


@lru_cache(maxsize=8)
def load_icon(size: int = 256) -> Image.Image:
    """Return an RGBA icon scaled to ``size`` (square)."""
    if ICON_PATH.is_file():
        img = Image.open(ICON_PATH).convert("RGBA")
    else:
        img = _draw_fallback(256)
    if img.size != (size, size):
        img = img.resize((size, size), Image.Resampling.LANCZOS)
    return img


def apply_tk_icon(root) -> None:
    """Set the Tk window / dock icon from the app PNG."""
    try:
        from PIL import ImageTk
    except ImportError:
        return
    # Keep a reference on root so PhotoImage is not GC'd
    photos = [ImageTk.PhotoImage(load_icon(size)) for size in (16, 32, 64, 128, 256)]
    root._mediactl_icons = photos  # type: ignore[attr-defined]
    try:
        root.iconphoto(True, *photos)
    except Exception:
        if photos:
            root.iconphoto(True, photos[-1])
