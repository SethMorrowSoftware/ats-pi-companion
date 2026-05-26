"""sd_notify helper tests — no systemd required."""
from __future__ import annotations

import asyncio
import os
import socket

import pytest

from atspi import notify as notify_mod


def test_notify_returns_false_outside_systemd(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert notify_mod.notify("READY=1") is False


def test_notify_sends_via_unix_socket(tmp_path, monkeypatch):
    sock_path = str(tmp_path / "notify.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(sock_path)
    server.settimeout(1.0)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", sock_path)
        assert notify_mod.notify("READY=1") is True
        data, _ = server.recvfrom(64)
        assert data == b"READY=1"
    finally:
        server.close()
        os.unlink(sock_path)


def test_notify_supports_abstract_socket_syntax(monkeypatch):
    """systemd may pass an @-prefixed abstract socket address."""
    if os.uname().sysname != "Linux":
        pytest.skip("abstract sockets are Linux-only")
    abstract_name = "test-atspi-notify-12345"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind("\0" + abstract_name)
    server.settimeout(1.0)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", "@" + abstract_name)
        assert notify_mod.notify("WATCHDOG=1") is True
        data, _ = server.recvfrom(64)
        assert data == b"WATCHDOG=1"
    finally:
        server.close()


async def test_watchdog_loop_exits_cleanly_without_systemd(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    # Should return immediately rather than spin forever.
    await asyncio.wait_for(notify_mod.watchdog_loop(0.01), timeout=0.5)
