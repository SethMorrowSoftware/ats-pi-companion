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


# ─── F1: hardware fail-safe gate wiring ──────────────────────────────────────


def test_build_io_driver_defaults_require_hw_watchdog_on():
    """The production driver path defaults the F1 gate ON, and threads the
    bench-verify register addresses from config to the driver.
    """
    from atspi.__main__ import _build_io_driver
    from atspi.config import Config

    cfg = Config()
    cfg.io.driver = "adam"
    cfg.io.adam.hw_watchdog.enable_register = 0x0100
    cfg.io.adam.hw_watchdog.timeout_register = 0x0101
    cfg.io.adam.hw_watchdog.safety_value_register_base = 0x0110
    driver = _build_io_driver(cfg)
    assert driver._require_hw_watchdog is True  # noqa: SLF001
    assert driver._hw_watchdog.enable_register == 0x0100  # noqa: SLF001
    # Fail closed until connect() verifies it.
    assert driver.hw_watchdog_ok() is False


async def test_sampling_loop_publishes_output_fault_when_hw_watchdog_unverified(monkeypatch):
    """F1: while the hardware fail-safe is unverified the sampling loop must
    keep OUTPUT_FAULT asserted so GenWatch sees a non-authoritative link.
    """
    from atspi import __main__ as main_mod
    from atspi.io_driver import InputSnapshot, OutputState
    from atspi.state import ADDR_FAULT_SUMMARY, FAULT_OUTPUT, RegisterStore

    monkeypatch.setattr(main_mod, "SAMPLE_INTERVAL_S", 0.01)

    class NotArmed:
        async def read_inputs(self):
            return InputSnapshot(
                position="utility", normal_available=True, emergency_available=True,
                engine_start_calling=False, ats_mode="auto", fault_bits=0,
            )

        async def read_output_state(self):
            return OutputState(False, False, False, False)

        async def release_all_outputs(self):
            pass

        def check_output_consistency(self, _actual):
            return True

        def hw_watchdog_ok(self):
            return False

    store = RegisterStore()
    task = asyncio.create_task(main_mod._sampling_loop(NotArmed(), store))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert store.read_register(ADDR_FAULT_SUMMARY) & FAULT_OUTPUT, "OUTPUT_FAULT must be set"


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


# ─── H-1: F1 fail-safe waiver requires an explicit second acknowledgement ─────


def test_waiver_without_ack_refuses_to_start():
    """require_hw_watchdog: false with no i_understand_no_crash_backstop ack
    must hard-fail: a one-line waiver should not silently remove the last
    crash-time backstop on the switch-command device.
    """
    import pytest

    from atspi.__main__ import _enforce_hw_watchdog_waiver
    from atspi.config import Config, ConfigError

    cfg = Config()
    cfg.io.driver = "adam"
    cfg.io.adam.require_hw_watchdog = False
    cfg.io.adam.i_understand_no_crash_backstop = False
    with pytest.raises(ConfigError, match="i_understand_no_crash_backstop"):
        _enforce_hw_watchdog_waiver(cfg)


def test_waiver_with_ack_starts():
    """The waiver is permitted once the operator sets the explicit ack key."""
    from atspi.__main__ import _enforce_hw_watchdog_waiver
    from atspi.config import Config

    cfg = Config()
    cfg.io.driver = "adam"
    cfg.io.adam.require_hw_watchdog = False
    cfg.io.adam.i_understand_no_crash_backstop = True
    _enforce_hw_watchdog_waiver(cfg)  # must not raise


def test_default_require_hw_watchdog_needs_no_ack():
    """The safe default (require_hw_watchdog: true) never needs the ack, and
    the mock driver is exempt regardless.
    """
    from atspi.__main__ import _enforce_hw_watchdog_waiver
    from atspi.config import Config

    cfg = Config()  # driver=mock, require_hw_watchdog=True
    _enforce_hw_watchdog_waiver(cfg)

    cfg.io.driver = "hybrid"  # require_hw_watchdog still True
    _enforce_hw_watchdog_waiver(cfg)

    cfg2 = Config()
    cfg2.io.driver = "mock"
    cfg2.io.adam.require_hw_watchdog = False  # mock is exempt
    cfg2.io.adam.i_understand_no_crash_backstop = False
    _enforce_hw_watchdog_waiver(cfg2)


# ─── ICD §9.3: reset-on-reboot output release ────────────────────────────────


async def test_sampling_loop_releases_outputs_at_startup(monkeypatch):
    """ICD §9.3: command outputs must be reset to released before the loop
    publishes anything. A relay latched by a previous service instance (a
    restart fast enough to beat the ADAM's host-idle watchdog) must not
    survive into this one.
    """
    from atspi import __main__ as main_mod
    from atspi.io_mock import IOMockDriver
    from atspi.state import ADDR_CMD_FORCE_TRANSFER_RB, ADDR_CMD_INHIBIT_RB, RegisterStore

    monkeypatch.setattr(main_mod, "SAMPLE_INTERVAL_S", 0.01)
    driver = IOMockDriver()
    await driver.connect()
    # Simulate relays left latched by a previous service instance.
    await driver.drive_outputs(inhibit=True, force_transfer=True)

    store = RegisterStore()
    task = asyncio.create_task(main_mod._sampling_loop(driver, store))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    out = await driver.read_output_state()
    assert out.inhibit_active is False, "stale inhibit must be released at startup"
    assert out.force_transfer_active is False
    assert store.read_register(ADDR_CMD_INHIBIT_RB) == 0
    assert store.read_register(ADDR_CMD_FORCE_TRANSFER_RB) == 0


async def test_sampling_loop_retries_startup_release_until_it_lands(monkeypatch):
    """An ADAM unreachable at boot must not skip the §9.3 reset — the loop
    retries the release each cycle (publishing INPUT_FAULT meanwhile) and
    only marks it done once the write lands.
    """
    from atspi import __main__ as main_mod
    from atspi.io_mock import IOMockDriver
    from atspi.state import RegisterStore

    monkeypatch.setattr(main_mod, "SAMPLE_INTERVAL_S", 0.01)

    class FlakyRelease(IOMockDriver):
        release_calls = 0
        fail_first = 3

        async def release_all_outputs(self):
            self.release_calls += 1
            if self.release_calls <= self.fail_first:
                raise OSError("ADAM unreachable")
            await super().release_all_outputs()

    driver = FlakyRelease()
    await driver.connect()
    await driver.drive_outputs(inhibit=True)

    store = RegisterStore()
    task = asyncio.create_task(main_mod._sampling_loop(driver, store))
    await asyncio.sleep(0.15)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert driver.release_calls >= driver.fail_first + 1, "release must be retried"
    out = await driver.read_output_state()
    assert out.inhibit_active is False, "release must eventually land"


# ─── Sampling-loop failure-log throttling ────────────────────────────────────


async def test_sense_failure_latches_input_fault_and_serves_last_good_position(monkeypatch):
    """ICD §10 "reachable but blind": when the input/sense read fails (e.g. the
    Group-5 RS-485 link drops) while the Modbus TCP server stays up, the
    producer MUST (a) latch INPUT_FAULT in fault_summary AND (b) keep serving
    its last-good position over the wire — not zeros, not 'unknown'. GenWatch
    then drops the ATS-Pi as authoritative. This pins the producer half that a
    regression (clearing the fault, or publishing a default snapshot in the
    except branch) would silently break.
    """
    from atspi import __main__ as main_mod
    from atspi.io_driver import InputSnapshot, OutputState
    from atspi.state import (
        ADDR_FAULT_SUMMARY,
        ADDR_POSITION,
        FAULT_INPUT,
        RegisterStore,
    )

    monkeypatch.setattr(main_mod, "SAMPLE_INTERVAL_S", 0.01)

    class Driver:
        fail = False

        async def read_inputs(self):
            if self.fail:
                raise OSError("Group-5 RS-485 read failed: TimeoutError")
            return InputSnapshot(
                position="generator", normal_available=False, emergency_available=True,
                engine_start_calling=True, ats_mode="auto", fault_bits=0,
            )

        async def read_output_state(self):
            return OutputState(False, False, False, False)

        async def release_all_outputs(self):
            pass

        def check_output_consistency(self, _actual):
            return True

        def hw_watchdog_ok(self):
            return True

    driver = Driver()
    store = RegisterStore()
    task = asyncio.create_task(main_mod._sampling_loop(driver, store))
    try:
        # First, a few healthy cycles establish position=generator (=1).
        await asyncio.sleep(0.05)
        assert store.read_register(ADDR_POSITION) == 1
        assert not (store.read_register(ADDR_FAULT_SUMMARY) & FAULT_INPUT)

        # Now the sense link drops while the server keeps running.
        driver.fail = True
        await asyncio.sleep(0.1)  # ~10 failing cycles

        # INPUT_FAULT is latched...
        assert store.read_register(ADDR_FAULT_SUMMARY) & FAULT_INPUT, "INPUT_FAULT not set"
        # ...and the last-good position is still served (not 0/utility, not unknown).
        assert store.read_register(ADDR_POSITION) == 1, "last-good position not preserved"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


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

        async def release_all_outputs(self):
            pass

        def check_output_consistency(self, _actual):
            return True

        def hw_watchdog_ok(self):
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
