"""Single-instance status window IPC."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from supervisor import status_ipc


@pytest.fixture
def short_sock(tmp_path_factory, monkeypatch):
    # AF_UNIX paths are short on macOS — keep under /tmp
    base = Path("/tmp") / f"mediactl_test_{tmp_path_factory.mktemp('x').name}"
    base.mkdir(parents=True, exist_ok=True)
    sock = base / "s.sock"
    monkeypatch.setattr(status_ipc, "STATUS_SOCK", sock)
    monkeypatch.setattr(status_ipc, "USER_CONFIG_DIR", base)
    yield sock
    if sock.exists():
        sock.unlink()


def test_raise_existing_false_when_no_server(short_sock):
    assert status_ipc.raise_existing() is False


def test_raise_existing_calls_handler(short_sock):
    hit = threading.Event()
    server = status_ipc.StatusRaiseServer(hit.set)
    assert server.start() is True
    try:
        assert status_ipc.raise_existing() is True
        assert hit.wait(1.0)
        other = status_ipc.StatusRaiseServer(lambda: None)
        assert other.start() is False
    finally:
        server.stop()
    assert status_ipc.raise_existing() is False
