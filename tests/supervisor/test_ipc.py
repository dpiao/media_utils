"""Tests for supervisor control socket IPC."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from supervisor import ipc

darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS unix socket focus")


@darwin_only
def test_control_server_status_roundtrip(monkeypatch):
    # AF_UNIX paths have a short max length — avoid long pytest tmp paths.
    sock = Path("/tmp/mediactl-test.sock")
    if sock.exists():
        sock.unlink()
    monkeypatch.setattr(ipc, "CONTROL_SOCK", sock)

    state = {"running": True}

    def handler(req):
        if req.get("cmd") == "status":
            return {
                "ok": True,
                "autostart": False,
                "workers": [{"name": "S3 Sync", "running": state["running"], "pid": 1, "uptime": "00:00:01"}],
            }
        if req.get("cmd") == "stop":
            state["running"] = False
            return {"ok": True}
        return {"ok": False, "error": "bad"}

    server = ipc.ControlServer(handler)
    server.start()
    try:
        time.sleep(0.1)
        status = ipc.request({"cmd": "status"})
        assert status["workers"][0]["name"] == "S3 Sync"
        assert status["workers"][0]["running"] is True
        assert ipc.request({"cmd": "stop", "worker": "S3 Sync"})["ok"] is True
        status2 = ipc.request({"cmd": "status"})
        assert status2["workers"][0]["running"] is False
    finally:
        server.stop()
    assert not sock.exists()
