"""Minimal sd_notify implementation for systemd integration.

When the service is launched under systemd with ``Type=notify``,
``$NOTIFY_SOCKET`` points at a UNIX datagram socket that accepts
status messages. We use it for:

  - ``READY=1`` — emit once the Modbus server is listening so systemd
    transitions us to "active (running)" rather than the unit
    transitioning the moment ExecStart returns.
  - ``WATCHDOG=1`` — periodic ping that the unit's ``WatchdogSec=``
    uses to detect a hung event loop. Missing the deadline restarts
    the service.

No-ops cleanly when running outside systemd (``$NOTIFY_SOCKET`` unset).
No external dependencies — just stdlib sockets.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket

log = logging.getLogger("atspi.notify")


def _socket_path() -> str | None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return None
    # systemd abstract socket: "@/foo/bar" → "\0/foo/bar"
    if addr.startswith("@"):
        return "\0" + addr[1:]
    return addr


def notify(message: str) -> bool:
    """Send a single sd_notify message. Returns True if delivered."""
    addr = _socket_path()
    if addr is None:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(message.encode("utf-8"), addr)
        return True
    except OSError as e:
        log.debug("sd_notify(%r) failed: %s", message, e)
        return False


def ready() -> None:
    """Tell systemd the service finished startup."""
    notify("READY=1")


def watchdog() -> None:
    """Ping the systemd watchdog (paired with WatchdogSec=)."""
    notify("WATCHDOG=1")


async def watchdog_loop(interval_s: float) -> None:
    """Background task: ping the watchdog at half-interval cadence."""
    addr = _socket_path()
    if addr is None:
        log.info("not running under systemd; watchdog pings disabled")
        return
    log.info("systemd watchdog pings every %.1fs", interval_s)
    while True:
        watchdog()
        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            return
