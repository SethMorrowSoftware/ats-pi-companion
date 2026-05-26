"""Safety watchdog tests (ICD §8.3).

These are correctness tests using a tunable timeout — the SPEC §5
also calls for a real-timer integration test, which is out of scope
for the unit suite (lives in docs/DEVELOPMENT.md §5).
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from atspi.io_mock import IOMockDriver
from atspi.safety import SafetyWatchdog
from atspi.state import ADDR_CMD_FORCE_TRANSFER_RB, ADDR_CMD_INHIBIT_RB, RegisterStore


@pytest.fixture
def short_timeout(monkeypatch):
    """Shrink the watchdog timing for fast tests."""
    import atspi.safety as safety_mod
    monkeypatch.setattr(safety_mod, "TIMEOUT_S", 0.2)
    monkeypatch.setattr(safety_mod, "CHECK_INTERVAL_S", 0.05)


async def test_watchdog_releases_after_timeout(short_timeout, caplog):
    caplog.set_level(logging.WARNING)
    store = RegisterStore()
    driver = IOMockDriver()
    await driver.connect()
    await driver.drive_outputs(inhibit=True, force_transfer=True)
    store.apply_output_state(await driver.read_output_state())
    assert store.read_register(ADDR_CMD_INHIBIT_RB) == 1

    watchdog = SafetyWatchdog(store, driver)
    task = asyncio.create_task(watchdog.run())
    try:
        # No note_modbus_read calls → silence → release fires.
        await asyncio.sleep(0.5)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Store reflects release.
    assert store.read_register(ADDR_CMD_INHIBIT_RB) == 0
    assert store.read_register(ADDR_CMD_FORCE_TRANSFER_RB) == 0
    # Driver was actually told to release.
    out = await driver.read_output_state()
    assert out.inhibit_active is False
    assert out.force_transfer_active is False


async def test_watchdog_does_not_release_when_reads_continue(short_timeout):
    store = RegisterStore()
    driver = IOMockDriver()
    await driver.connect()
    await driver.drive_outputs(inhibit=True)
    store.apply_output_state(await driver.read_output_state())

    watchdog = SafetyWatchdog(store, driver)
    task = asyncio.create_task(watchdog.run())
    try:
        # Tick read notes every 50 ms for 500 ms — well under the 200 ms
        # timeout per tick.
        for _ in range(10):
            await asyncio.sleep(0.05)
            watchdog.note_modbus_read()
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Inhibit still asserted.
    assert store.read_register(ADDR_CMD_INHIBIT_RB) == 1


async def test_watchdog_rearms_after_recovery(short_timeout):
    """Once a release has fired, a fresh read note must re-arm so a
    later silence triggers another release.
    """
    store = RegisterStore()
    driver = IOMockDriver()
    await driver.connect()
    await driver.drive_outputs(inhibit=True)
    store.apply_output_state(await driver.read_output_state())

    watchdog = SafetyWatchdog(store, driver)
    task = asyncio.create_task(watchdog.run())
    try:
        await asyncio.sleep(0.4)
        assert watchdog._released is True  # noqa: SLF001 (testing internal)

        # Comms back; re-arm
        watchdog.note_modbus_read()
        assert watchdog._released is False  # noqa: SLF001

        # Re-assert inhibit and wait for the next timeout
        await driver.drive_outputs(inhibit=True)
        store.apply_output_state(await driver.read_output_state())
        await asyncio.sleep(0.4)
        assert watchdog._released is True  # noqa: SLF001
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_watchdog_swallows_driver_errors(short_timeout):
    """If drive_outputs raises during release, the store is still
    cleared and the watchdog keeps running.
    """
    store = RegisterStore()
    driver = IOMockDriver()
    await driver.connect()
    await driver.drive_outputs(inhibit=True)
    store.apply_output_state(await driver.read_output_state())

    async def boom(**kwargs):
        raise OSError("simulated I/O failure")
    driver.drive_outputs = boom  # type: ignore[method-assign]

    watchdog = SafetyWatchdog(store, driver)
    task = asyncio.create_task(watchdog.run())
    try:
        await asyncio.sleep(0.4)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Store cleared even though driver failed.
    assert store.read_register(ADDR_CMD_INHIBIT_RB) == 0
    # Watchdog is still alive (we cancelled it, not the exception).
    # No exception leaked.


async def test_watchdog_keeps_retrying_until_drive_outputs_succeeds(short_timeout):
    """If the physical release write fails, the watchdog MUST NOT latch
    `_released=True` — otherwise an ADAM blip during a comms-loss event
    would leave inhibit / force-transfer asserted on the hardware forever.
    The next tick retries until the write lands.
    """
    store = RegisterStore()
    driver = IOMockDriver()
    await driver.connect()
    await driver.drive_outputs(inhibit=True, force_transfer=True)
    store.apply_output_state(await driver.read_output_state())

    real_drive = driver.drive_outputs
    fail_count = {"n": 0}

    async def flaky(**kwargs):
        if fail_count["n"] < 3:
            fail_count["n"] += 1
            raise OSError("simulated I/O failure")
        await real_drive(**kwargs)

    driver.drive_outputs = flaky  # type: ignore[method-assign]

    watchdog = SafetyWatchdog(store, driver)
    task = asyncio.create_task(watchdog.run())
    try:
        # Give the watchdog enough ticks (CHECK_INTERVAL_S=0.05) to fail
        # 3× and then succeed on the 4th attempt.
        await asyncio.sleep(0.6)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert fail_count["n"] == 3, "watchdog must retry on driver failure"
    # Latched only after the eventual successful drive.
    assert watchdog._released is True  # noqa: SLF001
    # Hardware is actually released now.
    out = await driver.read_output_state()
    assert out.inhibit_active is False
    assert out.force_transfer_active is False
