"""Unix-socket control API between mediactl and the status window process."""

from __future__ import annotations

import json
import logging
import socket
import threading
from pathlib import Path
from typing import Any, Callable

from supervisor.paths import CONTROL_SOCK

_log = logging.getLogger("mediactl")


class ControlServer:
    """Handles JSON-line requests on a Unix domain socket."""

    def __init__(self, handler: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self._handler = handler
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        CONTROL_SOCK.parent.mkdir(parents=True, exist_ok=True)
        if CONTROL_SOCK.exists():
            CONTROL_SOCK.unlink()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(CONTROL_SOCK))
        self._sock.listen(8)
        self._sock.settimeout(1.0)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        _log.info("Control socket listening: %s", CONTROL_SOCK)

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        if CONTROL_SOCK.exists():
            try:
                CONTROL_SOCK.unlink()
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
            buf = b""
            while True:
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        req = json.loads(line.decode("utf-8"))
                        resp = self._handler(req)
                    except Exception as exc:
                        _log.exception("Control handler error")
                        resp = {"ok": False, "error": str(exc)}
                    conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))


def request(cmd: dict[str, Any], timeout: float = 3.0) -> dict[str, Any]:
    """Send one command to the running mediactl control socket."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(CONTROL_SOCK))
        sock.sendall((json.dumps(cmd) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("control socket closed")
            buf += chunk
        line = buf.split(b"\n", 1)[0]
        return json.loads(line.decode("utf-8"))
    finally:
        sock.close()


def socket_path() -> Path:
    return CONTROL_SOCK
