"""Tests for the CLI entry point's shutdown-coordination helper.

Specifically, _wait_for_shutdown_or_failure must wake on either an explicit
stop signal OR the death of any critical background task — so a wedged
service doesn't sit idle waiting for SIGTERM after its Modbus server has
silently crashed.
"""
from __future__ import annotations

import asyncio

from atspi.__main__ import _wait_for_shutdown_or_failure


async def _forever() -> None:
    while True:
        await asyncio.sleep(60)


async def test_returns_shutdown_when_stop_event_fires():
    stop = asyncio.Event()
    crit = [asyncio.create_task(_forever(), name="crit-1")]
    try:
        stop_setter = asyncio.create_task(_set_after(stop, 0.05))
        reason = await asyncio.wait_for(
            _wait_for_shutdown_or_failure(stop, crit), timeout=1.0
        )
        await stop_setter
        assert reason == "shutdown"
    finally:
        for t in crit:
            t.cancel()


async def test_returns_task_name_when_critical_task_dies():
    """If a critical task raises, the helper returns its name so the
    main loop can log the failure and exit non-zero.
    """
    stop = asyncio.Event()

    async def boom() -> None:
        await asyncio.sleep(0.05)
        raise RuntimeError("modbus server crashed")

    crit = [
        asyncio.create_task(_forever(), name="sampling"),
        asyncio.create_task(boom(), name="modbus-server"),
    ]
    try:
        reason = await asyncio.wait_for(
            _wait_for_shutdown_or_failure(stop, crit), timeout=1.0
        )
        assert reason == "modbus-server"
    finally:
        for t in crit:
            t.cancel()


async def test_returns_task_name_when_critical_task_exits_cleanly():
    """A critical task that returns cleanly is still treated as a failure —
    they're expected to run forever.
    """
    stop = asyncio.Event()

    async def early_return() -> None:
        return None

    crit = [
        asyncio.create_task(_forever(), name="sampling"),
        asyncio.create_task(early_return(), name="safety-watchdog"),
    ]
    try:
        reason = await asyncio.wait_for(
            _wait_for_shutdown_or_failure(stop, crit), timeout=1.0
        )
        assert reason == "safety-watchdog"
    finally:
        for t in crit:
            t.cancel()


async def test_stop_task_is_cancelled_on_failure_path():
    """The internal stop-waiter task must not leak when the helper returns
    because of a task death.
    """
    stop = asyncio.Event()

    async def boom() -> None:
        await asyncio.sleep(0.02)
        raise RuntimeError("crash")

    crit = [asyncio.create_task(boom(), name="x")]
    try:
        await asyncio.wait_for(
            _wait_for_shutdown_or_failure(stop, crit), timeout=1.0
        )
        # Give the cancel one tick to take effect.
        await asyncio.sleep(0)
        # Now flip the stop event — nothing should still be waiting on it.
        stop.set()
        # If a leaked task is waiting, this sleep would let it run and
        # raise "Task was destroyed but it is pending!" warnings at the
        # next gc — best we can do is run a tick and exit cleanly.
        await asyncio.sleep(0)
    finally:
        for t in crit:
            t.cancel()


async def _set_after(ev: asyncio.Event, delay: float) -> None:
    await asyncio.sleep(delay)
    ev.set()
