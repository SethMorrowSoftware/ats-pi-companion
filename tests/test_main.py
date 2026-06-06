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


# ─── I/O driver construction from config ─────────────────────────────────────


def test_build_io_driver_rejects_invalid_assumed_mode():
    """A typo'd io.adam.assumed_mode must fail fast at startup, not silently
    report a bogus mode on the wire.
    """
    import pytest

    from atspi.__main__ import _build_io_driver
    from atspi.config import Config

    cfg = Config()
    cfg.io.driver = "adam"
    cfg.io.adam.assumed_mode = "bogus"
    with pytest.raises(ValueError, match="assumed_mode"):
        _build_io_driver(cfg)


def test_build_io_driver_rejects_invalid_di_read():
    """A typo'd io.adam.di_read must fail fast at startup, like assumed_mode."""
    import pytest

    from atspi.__main__ import _build_io_driver
    from atspi.config import Config

    cfg = Config()
    cfg.io.driver = "adam"
    cfg.io.adam.di_read = "bogus"
    with pytest.raises(ValueError, match="di_read"):
        _build_io_driver(cfg)


def test_build_io_driver_passes_di_read_through():
    """io.adam.di_read reaches the constructed ADAM driver."""
    from atspi.__main__ import _build_io_driver
    from atspi.config import Config

    cfg = Config()
    cfg.io.driver = "adam"
    cfg.io.adam.di_read = "discrete_inputs"
    driver = _build_io_driver(cfg)
    assert driver._di_read == "discrete_inputs"  # noqa: SLF001


def test_build_io_driver_mock_is_default():
    from atspi.__main__ import _build_io_driver
    from atspi.config import Config
    from atspi.io_mock import IOMockDriver

    assert isinstance(_build_io_driver(Config()), IOMockDriver)


# ─── site.unit_id default warning ────────────────────────────────────────────


def test_warns_when_site_unit_id_left_at_default(caplog):
    """Leaving site.unit_id at the default 1 collides with GenWatch's
    expected_unit_id check (ICD §5.4); startup must warn loudly.
    """
    import logging as _logging

    from atspi.__main__ import _warn_if_default_unit_id
    from atspi.config import Config

    cfg = Config()  # SiteCfg.unit_id defaults to 1
    caplog.set_level(_logging.WARNING, logger="atspi")
    _warn_if_default_unit_id(cfg)

    warnings = [
        r.getMessage() for r in caplog.records if r.levelno == _logging.WARNING
    ]
    assert any("site.unit_id" in m and "0x0035" in m for m in warnings), warnings


def test_no_warning_when_site_unit_id_is_configured(caplog):
    """A site that sets a real unit_id must not see the default-id warning."""
    import logging as _logging

    from atspi.__main__ import _warn_if_default_unit_id
    from atspi.config import Config

    cfg = Config()
    cfg.site.unit_id = 23
    caplog.set_level(_logging.WARNING, logger="atspi")
    _warn_if_default_unit_id(cfg)

    assert not any("site.unit_id" in r.getMessage() for r in caplog.records)


# ─── Sampling-loop failure-log throttling ────────────────────────────────────


async def test_sampling_loop_throttles_repeated_failure_logs(caplog, monkeypatch):
    """A sustained ADAM outage must not flood the journal at ~10 lines/s: the
    first failure of a streak logs once, repeats are throttled, and recovery
    logs once.
    """
    import logging as _logging

    from atspi import __main__ as main_mod
    from atspi.io_driver import InputSnapshot, OutputState
    from atspi.state import RegisterStore

    monkeypatch.setattr(main_mod, "SAMPLE_INTERVAL_S", 0.01)
    # Push the reminder cadence past the test window so we only see the first
    # failure + the recovery (no periodic reminders to count).
    monkeypatch.setattr(main_mod, "SAMPLING_FAILURE_REMINDER_S", 100.0)

    class Flaky:
        fail = True

        async def read_inputs(self):
            if self.fail:
                raise OSError("ADAM read_coils(0, 6) failed: TimeoutError")
            return InputSnapshot(
                position="utility", normal_available=True, emergency_available=True,
                engine_start_calling=False, ats_mode="auto", fault_bits=0,
            )

        async def read_output_state(self):
            return OutputState(False, False, False, False)

        def check_output_consistency(self, _actual):
            return True

    driver = Flaky()
    store = RegisterStore()
    caplog.set_level(_logging.INFO, logger="atspi")
    task = asyncio.create_task(main_mod._sampling_loop(driver, store))
    await asyncio.sleep(0.2)   # ~20 failing cycles
    driver.fail = False
    await asyncio.sleep(0.05)  # let it recover
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    msgs = [r.getMessage() for r in caplog.records]
    first = [m for m in msgs if m.startswith("sampling cycle failed")]
    reminders = [m for m in msgs if m.startswith("sampling still failing")]
    recovered = [m for m in msgs if m.startswith("sampling recovered")]
    assert len(first) == 1, f"expected one first-failure line over ~20 cycles, got {first}"
    assert reminders == [], "no reminder should fire inside this short window"
    assert len(recovered) == 1, f"expected one recovery line, got {recovered}"
