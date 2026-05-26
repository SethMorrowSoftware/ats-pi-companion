"""Tests for the mock driver's SIGUSR1/SIGUSR2 runtime controls.

These verify the signal-handler wiring end-to-end: send a real signal
to our own process, give the event loop a tick to dispatch it, observe
the mock's state changed.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys

import pytest

from atspi.io_mock import IOMockDriver

# Signal handlers are POSIX-only; SIGUSR1/2 don't exist on Windows.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="SIGUSR1/2 not supported on Windows"
)


async def test_cycle_position_directly():
    """Direct call (no signal) — covers the deterministic cycle."""
    d = IOMockDriver()
    assert d.position == "utility"
    d.cycle_position()
    assert d.position == "generator"
    d.cycle_position()
    assert d.position == "transferring"
    d.cycle_position()
    assert d.position == "unknown"
    d.cycle_position()
    assert d.position == "utility"  # wraps


async def test_toggle_normal_available_directly():
    d = IOMockDriver()
    assert d.normal_available is True
    assert d.engine_start_calling is False
    d.toggle_normal_available()
    assert d.normal_available is False
    assert d.engine_start_calling is True
    d.toggle_normal_available()
    assert d.normal_available is True
    assert d.engine_start_calling is False


async def test_sigusr1_cycles_position_via_signal():
    """Send a real SIGUSR1 to our own process; the loop dispatches it
    to the registered handler and we observe the state change.
    """
    d = IOMockDriver()
    await d.connect()
    try:
        assert d.position == "utility"
        os.kill(os.getpid(), signal.SIGUSR1)
        # The signal handler runs at the next loop tick.
        await asyncio.sleep(0.05)
        assert d.position == "generator"

        os.kill(os.getpid(), signal.SIGUSR1)
        await asyncio.sleep(0.05)
        assert d.position == "transferring"
    finally:
        await d.close()


async def test_sigusr2_toggles_normal_available_via_signal():
    d = IOMockDriver()
    await d.connect()
    try:
        os.kill(os.getpid(), signal.SIGUSR2)
        await asyncio.sleep(0.05)
        assert d.normal_available is False
        assert d.engine_start_calling is True
    finally:
        await d.close()


async def test_close_removes_signal_handlers():
    """After close(), sending the signal must not crash the loop and
    must not affect mock state (handler is gone).
    """
    d = IOMockDriver()
    await d.connect()
    await d.close()
    # If the close didn't remove the handler, this signal would still
    # call cycle_position. Default Python signal behaviour for SIGUSR1
    # without a handler is to terminate the process; install a no-op
    # signal.signal() handler so we can fire it safely.
    signal.signal(signal.SIGUSR1, signal.SIG_IGN)
    try:
        os.kill(os.getpid(), signal.SIGUSR1)
        await asyncio.sleep(0.05)
        assert d.position == "utility"  # untouched
    finally:
        signal.signal(signal.SIGUSR1, signal.SIG_DFL)


async def test_connect_outside_event_loop_is_safe(monkeypatch):
    """If a driver is constructed and connect() is somehow called
    outside an event loop, signal-handler install is a quiet no-op.
    """
    d = IOMockDriver()

    # asyncio.get_running_loop raises RuntimeError when no loop is running.
    # Simulate by patching it to raise.
    def boom():
        raise RuntimeError("no running loop")

    monkeypatch.setattr("atspi.io_mock.asyncio.get_running_loop", boom)
    # connect() must still return True without raising.
    ok = await d.connect()
    assert ok is True
