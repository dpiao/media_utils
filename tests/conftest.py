import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
WINDOWS = SRC / "windows"
for path in (SRC, WINDOWS):
    p = str(path)
    if p not in sys.path:
        sys.path.insert(0, p)
