"""Single-instance status window: raise existing viewer or start a new one."""

from __future__ import annotations

import json
import logging
import socket
import threading
from typing import Callable

from supervisor.paths import USER_CONFIG_DIR

STATUS_SOCK = USER_CONFIG_DIR / "mediactl_status.sock"
_log = logging.getLogger("mediactl")


def raise_existing(timeout: float = 0.4) -> bool:
    """Ask a running status viewer to come to front. True if one answered."""
    if not STATUS_SOCK.exists():
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(STATUS_SOCK))
        sock.sendall(b'{"cmd":"raise"}\n')
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return False
            buf += chunk
        resp = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
        return bool(resp.get("ok"))
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


class StatusRaiseServer:
    """Unix socket that accepts ``raise`` while the status window is open."""

    def __init__(self, on_raise: Callable[[], None]) -> None:
        self._on_raise = on_raise
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> bool:
        """Bind STATUS_SOCK. Returns False if another viewer already owns it."""
        USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if STATUS_SOCK.exists():
            if raise_existing():
                return False
            try:
                STATUS_SOCK.unlink()
            except OSError:
                pass
        try:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.bind(str(STATUS_SOCK))
            self._sock.listen(4)
            self._sock.settimeout(1.0)
        except OSError as exc:
            _log.warning("Could not bind status socket: %s", exc)
            if raise_existing():
                return False
            raise
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        if STATUS_SOCK.exists():
            try:
                STATUS_SOCK.unlink()
            except OSError:
                pass

    def _loop(self) -> None:
        assert self._sock
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            try:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                req = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
                if req.get("cmd") == "raise":
                    self._on_raise()
                    conn.sendall(b'{"ok":true}\n')
                else:
                    conn.sendall(b'{"ok":false,"error":"unknown"}\n')
            except Exception as exc:
                _log.warning("Status raise handler error: %s", exc)
                try:
                    conn.sendall(b'{"ok":false}\n')
                except OSError:
                    pass
