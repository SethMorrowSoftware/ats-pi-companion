"""ADAM-6060 driver tests using a fake pymodbus client.

These verify the bit-decoding and channel-mapping logic without
needing real hardware. Bench verification of the ADAM register map
itself is documented in docs/SPEC.md §8 phase E.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from atspi.io_adam import (
    DI_COIL_BASE,
    DI_EMERGENCY_AVAIL,
    DI_ENGINE_START,
    DI_LOAD_DISCONNECT,
    DI_NORMAL_AVAIL,
    DI_ON_EMERGENCY,
    DI_ON_NORMAL,
    DO_BYPASS_DELAY,
    DO_COIL_BASE,
    DO_FORCE_TRANSFER,
    DO_INHIBIT,
    DO_TEST,
    HwWatchdogConfig,
    HwWatchdogNotArmedError,
    IOAdamDriver,
)


async def _wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    """Poll predicate at 20 ms intervals until it returns truthy or timeout
    expires. Replaces fixed-sleep waits that became flaky on slow CI runners.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"predicate did not become true within {timeout}s")


@dataclass
class FakeResult:
    bits: list[bool] = field(default_factory=list)
    registers: list[int] = field(default_factory=list)
    is_err: bool = False

    def isError(self) -> bool:  # noqa: N802 (pymodbus interface)
        return self.is_err


class FakeClient:
    """Stand-in for AsyncModbusTcpClient. Records writes and serves
    canned reads.
    """

    def __init__(self):
        self.connected = False
        self.di_bits = [False] * 6
        self.do_bits = [False] * 6
        self.writes: list[tuple[int, bool]] = []
        # (fn_name, address) of every bit read, so tests can assert which
        # function code a DI read used (FC01 read_coils vs FC02 discrete inputs).
        self.reads: list[tuple[str, int]] = []
        # Holding registers for the F1 host-watchdog readback (addr -> value).
        # Unset addresses read 0; set hr_raises to simulate a Modbus error.
        self.holding_registers: dict[int, int] = {}
        self.hr_raises: bool = False

    async def connect(self) -> bool:
        self.connected = True
        return True

    def close(self) -> None:
        self.connected = False

    async def read_holding_registers(self, address, count, slave):
        self.reads.append(("read_holding_registers", address))
        if self.hr_raises:
            from pymodbus.exceptions import ModbusIOException
            raise ModbusIOException("simulated holding-register read failure")
        regs = [self.holding_registers.get(address + i, 0) for i in range(count)]
        return FakeResult(registers=regs)

    @staticmethod
    def _pad_to_byte(bits):
        # pymodbus rounds bits up to byte; replicate that quirk.
        while len(bits) % 8 != 0:
            bits.append(False)
        return bits

    async def read_coils(self, address, count, slave):
        self.reads.append(("read_coils", address))
        if address == DI_COIL_BASE:
            bits = list(self.di_bits[:count])
        elif address == DO_COIL_BASE:
            bits = list(self.do_bits[:count])
        else:
            bits = [False] * count
        return FakeResult(bits=self._pad_to_byte(bits))

    async def read_discrete_inputs(self, address, count, slave):
        # The ADAM exposes DIs in the discrete-input space; serve di_bits.
        self.reads.append(("read_discrete_inputs", address))
        bits = list(self.di_bits[:count]) if address == DI_COIL_BASE else [False] * count
        return FakeResult(bits=self._pad_to_byte(bits))

    async def write_coil(self, address, value, slave):
        self.writes.append((address, value))
        idx = address - DO_COIL_BASE
        if 0 <= idx < len(self.do_bits):
            self.do_bits[idx] = value
        return FakeResult()


@pytest.fixture
def driver():
    # These tests exercise the I/O decode/drive/pulse logic, not the F1
    # hardware-fail-safe gate — waive it so drive_outputs isn't refused. The
    # gate itself is covered by the dedicated tests further down.
    d = IOAdamDriver(host="127.0.0.1", port=5020, unit_id=1, require_hw_watchdog=False)
    d._client = FakeClient()  # noqa: SLF001
    d._connected = True  # noqa: SLF001
    return d


async def test_read_inputs_position_utility(driver):
    fake = driver._client  # noqa: SLF001
    fake.di_bits[DI_ON_NORMAL] = True
    fake.di_bits[DI_NORMAL_AVAIL] = True
    fake.di_bits[DI_EMERGENCY_AVAIL] = True
    snap = await driver.read_inputs()
    assert snap.position == "utility"
    assert snap.normal_available is True
    assert snap.emergency_available is True
    assert snap.engine_start_calling is False


async def test_read_inputs_position_generator(driver):
    fake = driver._client  # noqa: SLF001
    fake.di_bits[DI_ON_EMERGENCY] = True
    fake.di_bits[DI_NORMAL_AVAIL] = False
    fake.di_bits[DI_EMERGENCY_AVAIL] = True
    fake.di_bits[DI_ENGINE_START] = True
    snap = await driver.read_inputs()
    assert snap.position == "generator"
    assert snap.normal_available is False
    assert snap.engine_start_calling is True


async def test_read_inputs_position_transferring_via_load_disconnect_pulse(driver):
    fake = driver._client  # noqa: SLF001
    # Mid-stroke: neither aux contact closed, but load disconnect just pulsed.
    fake.di_bits[DI_LOAD_DISCONNECT] = True
    snap = await driver.read_inputs()
    assert snap.position == "transferring"

    # Pulse drops; still reports transferring within the hold window.
    fake.di_bits[DI_LOAD_DISCONNECT] = False
    snap2 = await driver.read_inputs()
    assert snap2.position == "transferring"


async def test_read_inputs_position_unknown_when_no_aux_and_no_pulse(driver):
    # Neither aux closed and no recent load-disconnect pulse.
    snap = await driver.read_inputs()
    assert snap.position == "unknown"


async def test_drive_outputs_maintained_inhibit(driver):
    fake = driver._client  # noqa: SLF001
    await driver.drive_outputs(inhibit=True)
    assert (DO_COIL_BASE + DO_INHIBIT, True) in fake.writes

    await driver.drive_outputs(inhibit=False)
    assert (DO_COIL_BASE + DO_INHIBIT, False) in fake.writes


async def test_drive_outputs_maintained_force_transfer(driver):
    fake = driver._client  # noqa: SLF001
    await driver.drive_outputs(force_transfer=True)
    assert (DO_COIL_BASE + DO_FORCE_TRANSFER, True) in fake.writes


async def test_drive_outputs_test_pulse_clamps_and_self_clears(driver):
    fake = driver._client  # noqa: SLF001
    # Request something shorter than the ICD minimum; should clamp up.
    await driver.drive_outputs(test_pulse_ms=100)
    assert (DO_COIL_BASE + DO_TEST, True) in fake.writes

    # Poll for the release write (driver clamps to 500 ms min).
    await _wait_for(lambda: (DO_COIL_BASE + DO_TEST, False) in fake.writes)


async def test_drive_outputs_bypass_pulse_self_clears(driver):
    fake = driver._client  # noqa: SLF001
    await driver.drive_outputs(bypass_delay_pulse_ms=500)
    assert (DO_COIL_BASE + DO_BYPASS_DELAY, True) in fake.writes
    await _wait_for(lambda: (DO_COIL_BASE + DO_BYPASS_DELAY, False) in fake.writes)


async def test_test_pulse_re_trigger_during_active_is_ignored(driver):
    """ICD §6: 'Writes while cmd_test_active=1 are ignored
    (no re-triggering mid-pulse).'
    """
    fake = driver._client  # noqa: SLF001
    # Start a max-length pulse so we have time to attempt a re-trigger.
    await driver.drive_outputs(test_pulse_ms=1500)
    first_writes = list(fake.writes)
    # Issue another pulse while the first is in flight.
    await driver.drive_outputs(test_pulse_ms=1500)
    # No additional coil write should have happened — the second call is a no-op.
    assert fake.writes == first_writes, (
        "Re-trigger during active pulse must not write to the coil again"
    )
    # Original pulse still releases on schedule.
    await asyncio.sleep(1.7)
    assert (DO_COIL_BASE + DO_TEST, False) in fake.writes


async def test_bypass_pulse_re_trigger_during_active_is_ignored(driver):
    """Same idempotency rule for bypass_delay (ICD §6)."""
    fake = driver._client  # noqa: SLF001
    await driver.drive_outputs(bypass_delay_pulse_ms=1500)
    first_writes = list(fake.writes)
    await driver.drive_outputs(bypass_delay_pulse_ms=1500)
    assert fake.writes == first_writes


async def test_test_pulse_can_be_re_issued_after_completion(driver):
    """After the original pulse self-clears, a new pulse must be accepted."""
    fake = driver._client  # noqa: SLF001
    await driver.drive_outputs(test_pulse_ms=500)
    # Wait long enough for the auto-release to fire.
    await asyncio.sleep(0.7)
    writes_after_first = len(fake.writes)
    # Now a fresh pulse must take effect.
    await driver.drive_outputs(test_pulse_ms=500)
    assert len(fake.writes) > writes_after_first, (
        "After the original pulse completed, a new pulse must drive the coil"
    )


async def test_read_output_state_decodes_bits(driver):
    fake = driver._client  # noqa: SLF001
    fake.do_bits[DO_TEST] = True
    fake.do_bits[DO_INHIBIT] = True
    out = await driver.read_output_state()
    assert out.test_active is True
    assert out.inhibit_active is True
    assert out.force_transfer_active is False
    assert out.bypass_delay_active is False


# ─── release_all_outputs (ICD §9.3 reset + shutdown/bench cleanup) ────────


async def test_release_all_outputs_writes_all_four_command_dos_off(driver):
    fake = driver._client  # noqa: SLF001
    fake.do_bits[DO_INHIBIT] = True
    fake.do_bits[DO_FORCE_TRANSFER] = True
    await driver.release_all_outputs()
    for do in (DO_TEST, DO_FORCE_TRANSFER, DO_INHIBIT, DO_BYPASS_DELAY):
        assert (DO_COIL_BASE + do, False) in fake.writes, f"DO{do} must be driven OFF"
    # Spares are not ours to touch (HARDWARE.md §3).
    assert not any(addr in (DO_COIL_BASE + 4, DO_COIL_BASE + 5) for addr, _ in fake.writes)
    # Recorded as commanded-OFF so stuck-relay detection covers a relay that
    # fails to drop.
    for do in (DO_TEST, DO_FORCE_TRANSFER, DO_INHIBIT, DO_BYPASS_DELAY):
        assert driver._commanded_do[do][0] is False  # noqa: SLF001


async def test_release_all_outputs_cancels_inflight_pulse(driver):
    """A pulse mid-flight must not re-assert (or double-release) behind the
    all-off write — its release timer is cancelled first.
    """
    fake = driver._client  # noqa: SLF001
    await driver.drive_outputs(test_pulse_ms=1500)
    assert (DO_COIL_BASE + DO_TEST, True) in fake.writes
    await driver.release_all_outputs()
    task = driver._test_release_task  # noqa: SLF001
    await asyncio.sleep(0)  # let the cancellation propagate
    assert task.cancelled() or task.done()
    assert (DO_COIL_BASE + DO_TEST, False) in fake.writes
    # The pulse slot is reusable afterwards.
    await driver.drive_outputs(test_pulse_ms=500)
    assert fake.writes.count((DO_COIL_BASE + DO_TEST, True)) == 2


async def test_release_all_outputs_allowed_while_hw_watchdog_unverified():
    """F1: releases must never be blocked by an unverified hardware
    fail-safe — this is what lets the startup/shutdown reset run.
    """
    d = IOAdamDriver(host="127.0.0.1", port=5020, unit_id=1, require_hw_watchdog=True)
    d._client = FakeClient()  # noqa: SLF001
    d._connected = True  # noqa: SLF001
    assert d.hw_watchdog_ok() is False
    await d.release_all_outputs()  # must not raise HwWatchdogNotArmedError
    assert (DO_COIL_BASE + DO_INHIBIT, False) in d._client.writes  # noqa: SLF001


# ─── Stuck-relay detection ───────────────────────────────────────────────


async def test_check_output_consistency_passes_within_settling_window(driver):
    """Right after a write, the ADAM may not yet reflect the new state.
    The settling window suppresses false positives.
    """
    fake = driver._client  # noqa: SLF001
    # Drive inhibit on, but pretend the read-back hasn't caught up yet.
    await driver.drive_outputs(inhibit=True)
    fake.do_bits[DO_INHIBIT] = False  # simulate "ADAM scan hasn't refreshed"
    actual = await driver.read_output_state()
    # Within settling window → no fault.
    assert driver.check_output_consistency(actual) is True


async def test_check_output_consistency_detects_stuck_relay(driver, monkeypatch):
    """Past the settling window, a commanded-vs-actual mismatch is a
    stuck-relay fault.
    """
    import atspi.io_adam as io_adam_mod
    monkeypatch.setattr(io_adam_mod, "OUTPUT_SETTLING_S", 0.05)
    fake = driver._client  # noqa: SLF001
    # Drive inhibit on; relay sticks off (simulate broken DO 2).
    await driver.drive_outputs(inhibit=True)
    fake.do_bits[DO_INHIBIT] = False
    await asyncio.sleep(0.1)  # exceed settling window
    actual = await driver.read_output_state()
    assert driver.check_output_consistency(actual) is False


async def test_check_output_consistency_passes_when_relays_match(driver, monkeypatch):
    """Past the settling window, matching commanded + actual → no fault."""
    import atspi.io_adam as io_adam_mod
    monkeypatch.setattr(io_adam_mod, "OUTPUT_SETTLING_S", 0.05)
    # FakeClient mirrors writes into do_bits so actual==commanded.
    await driver.drive_outputs(inhibit=True, force_transfer=False)
    await asyncio.sleep(0.1)
    actual = await driver.read_output_state()
    assert driver.check_output_consistency(actual) is True


async def test_check_output_consistency_returns_true_when_nothing_commanded(driver):
    """Fresh driver, no commands issued — nothing to verify."""
    actual = await driver.read_output_state()
    assert driver.check_output_consistency(actual) is True


async def test_check_output_consistency_tracks_pulse_release(driver, monkeypatch):
    """After a pulse self-releases, the commanded state flips to False;
    a still-asserted read-back becomes a stuck-relay fault.
    """
    import atspi.io_adam as io_adam_mod
    monkeypatch.setattr(io_adam_mod, "OUTPUT_SETTLING_S", 0.05)
    fake = driver._client  # noqa: SLF001
    await driver.drive_outputs(test_pulse_ms=500)
    # Wait for the auto-release write to fire.
    deadline = asyncio.get_event_loop().time() + 2.0
    while asyncio.get_event_loop().time() < deadline:
        if (DO_COIL_BASE + DO_TEST, False) in fake.writes:
            break
        await asyncio.sleep(0.02)
    # Simulate the test relay sticking on past release.
    fake.do_bits[DO_TEST] = True
    await asyncio.sleep(0.1)  # exceed settling window after release
    actual = await driver.read_output_state()
    assert driver.check_output_consistency(actual) is False


async def test_read_failure_marks_disconnected_and_raises():
    d = IOAdamDriver(host="127.0.0.1", port=5020)

    class FailingClient:
        connected = True
        async def connect(self): return True
        def close(self): pass
        async def read_coils(self, **kwargs):
            from pymodbus.exceptions import ModbusIOException
            raise ModbusIOException("simulated")

    d._client = FailingClient()  # noqa: SLF001
    d._connected = True  # noqa: SLF001
    with pytest.raises(IOError):
        await d.read_inputs()
    assert d._connected is False  # noqa: SLF001


# ─── Pulse-release robustness (stranded-relay prevention) ────────────────────


class _ReleaseFailClient(FakeClient):
    """FakeClient whose release writes (value=False) fail a configurable
    number of times before succeeding. Simulates an ADAM/network blip landing
    on the exact instant a pulsed relay is being released.
    """

    def __init__(self, fail_releases: int):
        super().__init__()
        self._fail_releases = fail_releases

    async def write_coil(self, address, value, slave):
        if value is False and self._fail_releases > 0:
            self._fail_releases -= 1
            raise OSError("simulated ADAM blip on release write")
        return await super().write_coil(address, value, slave)


async def test_pulse_release_retries_until_it_lands(driver, monkeypatch):
    """A transiently-failing release write MUST be retried until it lands — a
    momentary relay (Test, Bypass) can never be left stranded ON. Leaving the
    Test relay asserted would continuously command the ATS to test-transfer.
    """
    import atspi.io_adam as io_adam_mod
    monkeypatch.setattr(io_adam_mod, "PULSE_RELEASE_RETRY_S", 0.02)
    # Three release writes fail, the fourth succeeds.
    driver._client = _ReleaseFailClient(fail_releases=3)  # noqa: SLF001
    driver._connected = True  # noqa: SLF001
    # Isolate the retry logic from the real-socket reconnect path that a
    # failed write would otherwise trigger (that path is covered separately).
    async def _noop():
        return
    monkeypatch.setattr(driver, "_ensure_connected", _noop)

    await driver.drive_outputs(test_pulse_ms=500)
    assert driver._client.do_bits[DO_TEST] is True  # noqa: SLF001  (asserted)
    # The pulse window elapses, the first releases fail, then one lands.
    await _wait_for(lambda: driver._client.do_bits[DO_TEST] is False, timeout=3.0)  # noqa: SLF001


async def test_stranded_pulse_relay_surfaces_as_output_fault(driver, monkeypatch):
    """If the release write keeps failing the relay stays ON — but the driver
    MUST record the intended OFF state at pulse expiry so stuck-relay detection
    raises a fault (commanded=False vs actual=True past the settling window)
    instead of silently masking the stranded relay (commanded==actual==True).
    """
    import atspi.io_adam as io_adam_mod
    monkeypatch.setattr(io_adam_mod, "OUTPUT_SETTLING_S", 0.05)
    monkeypatch.setattr(io_adam_mod, "PULSE_RELEASE_RETRY_S", 0.02)
    driver._client = _ReleaseFailClient(fail_releases=10_000)  # noqa: SLF001  (never lands)
    driver._connected = True  # noqa: SLF001
    async def _noop():
        return
    monkeypatch.setattr(driver, "_ensure_connected", _noop)

    await driver.drive_outputs(test_pulse_ms=500)
    # Past the pulse window (500 ms) + the settling window (50 ms).
    await asyncio.sleep(0.7)
    actual = await driver.read_output_state()
    assert actual.test_active is True, "release writes failing → relay stuck on"
    assert driver.check_output_consistency(actual) is False, (
        "a pulse stranded ON past its window must raise a stuck-relay fault, "
        "not be silently masked by stale commanded state"
    )


# ─── Input debounce ──────────────────────────────────────────────────────────


async def test_first_read_seeds_baseline_without_debounce_delay(driver):
    """The first read must publish the true state immediately — no startup
    delay waiting out the debounce window.
    """
    fake = driver._client  # noqa: SLF001
    fake.di_bits[DI_ON_EMERGENCY] = True
    snap = await driver.read_inputs()
    assert snap.position == "generator"


async def test_debounce_rejects_single_sample_glitch(driver):
    """A level input that flips for a single sample must NOT change published
    state (driver default is 3 consecutive samples).
    """
    fake = driver._client  # noqa: SLF001
    fake.di_bits[DI_ON_NORMAL] = True
    fake.di_bits[DI_NORMAL_AVAIL] = True
    fake.di_bits[DI_EMERGENCY_AVAIL] = True
    assert (await driver.read_inputs()).position == "utility"  # seeds baseline

    # One-sample glitch: normal_available drops for exactly one read, then back.
    fake.di_bits[DI_NORMAL_AVAIL] = False
    assert (await driver.read_inputs()).normal_available is True, (
        "single-sample glitch must be debounced away"
    )
    fake.di_bits[DI_NORMAL_AVAIL] = True
    assert (await driver.read_inputs()).normal_available is True


async def test_debounce_accepts_sustained_change(driver):
    """A change held for the full debounce window IS published."""
    fake = driver._client  # noqa: SLF001
    fake.di_bits[DI_NORMAL_AVAIL] = True
    assert (await driver.read_inputs()).normal_available is True  # seed baseline

    fake.di_bits[DI_NORMAL_AVAIL] = False
    # Default debounce = 3 samples: not published until the 3rd consecutive read.
    assert (await driver.read_inputs()).normal_available is True
    assert (await driver.read_inputs()).normal_available is True
    assert (await driver.read_inputs()).normal_available is False


async def test_load_disconnect_pulse_is_not_debounced(driver):
    """DI 0 is momentary — a single raw high must register 'transferring'
    immediately, despite the debounce applied to the other channels.
    """
    fake = driver._client  # noqa: SLF001
    fake.di_bits[DI_ON_NORMAL] = True
    await driver.read_inputs()  # seed baseline on utility
    fake.di_bits[DI_LOAD_DISCONNECT] = True
    snap = await driver.read_inputs()
    assert snap.position == "transferring", (
        "a momentary DI0 pulse must not be debounced away"
    )


async def test_debounce_samples_one_disables_debounce():
    d = IOAdamDriver(host="127.0.0.1", port=5020, debounce_samples=1)
    d._client = FakeClient()  # noqa: SLF001
    d._connected = True  # noqa: SLF001
    fake = d._client  # noqa: SLF001
    fake.di_bits[DI_NORMAL_AVAIL] = True
    await d.read_inputs()  # seed
    fake.di_bits[DI_NORMAL_AVAIL] = False
    assert (await d.read_inputs()).normal_available is False, (
        "debounce_samples=1 must publish a change on the very next read"
    )


# ─── Assumed mode (no Auto/Manual sense contact) ─────────────────────────────


async def test_assumed_mode_is_reported_in_snapshot():
    d = IOAdamDriver(host="127.0.0.1", port=5020, assumed_mode="manual")
    d._client = FakeClient()  # noqa: SLF001
    d._connected = True  # noqa: SLF001
    snap = await d.read_inputs()
    assert snap.ats_mode == "manual"


async def test_assumed_mode_defaults_to_auto(driver):
    snap = await driver.read_inputs()
    assert snap.ats_mode == "auto"


def test_invalid_assumed_mode_raises():
    with pytest.raises(ValueError, match="assumed_mode"):
        IOAdamDriver(host="127.0.0.1", port=5020, assumed_mode="bogus")


# ─── DI read function code (FC01 coils vs FC02 discrete inputs) ───────────────


async def test_default_di_read_uses_read_coils(driver):
    """Default keeps the historical FC01 (read_coils) DI read."""
    fake = driver._client  # noqa: SLF001
    await driver.read_inputs()
    assert ("read_coils", DI_COIL_BASE) in fake.reads
    assert ("read_discrete_inputs", DI_COIL_BASE) not in fake.reads


async def test_di_read_discrete_inputs_uses_fc02():
    """di_read='discrete_inputs' reads the DIs via FC02 (read_discrete_inputs),
    not FC01 — the escape hatch for ADAM firmware that maps DIs to the
    discrete-input space.
    """
    d = IOAdamDriver(host="127.0.0.1", port=5020, di_read="discrete_inputs")
    fake = FakeClient()
    d._client = fake  # noqa: SLF001
    d._connected = True  # noqa: SLF001
    fake.di_bits[DI_ON_EMERGENCY] = True
    snap = await d.read_inputs()
    # Decoding still works through the FC02 path.
    assert snap.position == "generator"
    assert ("read_discrete_inputs", DI_COIL_BASE) in fake.reads
    assert ("read_coils", DI_COIL_BASE) not in fake.reads


async def test_di_read_discrete_inputs_leaves_do_readback_on_coils():
    """Switching the DI read to FC02 must NOT move the DO read-back off FC01 —
    relays are always coils.
    """
    d = IOAdamDriver(host="127.0.0.1", port=5020, di_read="discrete_inputs")
    fake = FakeClient()
    d._client = fake  # noqa: SLF001
    d._connected = True  # noqa: SLF001
    fake.do_bits[DO_INHIBIT] = True
    out = await d.read_output_state()
    assert out.inhibit_active is True
    assert ("read_coils", DO_COIL_BASE) in fake.reads


def test_invalid_di_read_raises():
    with pytest.raises(ValueError, match="di_read"):
        IOAdamDriver(host="127.0.0.1", port=5020, di_read="bogus")


# ─── F1: ADAM hardware host-watchdog fail-safe self-check ─────────────────────

# Example BENCH-VERIFY register layout used by these tests. The real addresses
# come from the ADAM-6000 User Manual (Appendix B) and are confirmed on the
# unit; the driver logic is identical regardless of the actual numbers.
_WD_ENABLE_REG = 0x0100
_WD_TIMEOUT_REG = 0x0101
_WD_SAFETY_BASE = 0x0110


def _armed_config() -> HwWatchdogConfig:
    return HwWatchdogConfig(
        enable_register=_WD_ENABLE_REG,
        enable_expected=1,
        timeout_register=_WD_TIMEOUT_REG,
        timeout_scale_s=0.1,
        timeout_min_s=5.0,
        timeout_max_s=10.0,
        safety_value_register_base=_WD_SAFETY_BASE,
        safety_value_count=6,
    )


def _armed_registers() -> dict[int, int]:
    # Enabled, 7.0 s timeout (raw 70 × 0.1), all six DO safety values OFF.
    return {_WD_ENABLE_REG: 1, _WD_TIMEOUT_REG: 70}


_USE_ARMED_CONFIG = object()  # sentinel: distinct from None (= unconfigured)


async def _connect_with_registers(
    registers: dict[int, int],
    *,
    require: bool = True,
    config: HwWatchdogConfig | None = _USE_ARMED_CONFIG,
    hr_raises: bool = False,
) -> IOAdamDriver:
    if config is _USE_ARMED_CONFIG:
        config = _armed_config()
    d = IOAdamDriver(
        host="127.0.0.1", port=5020,
        require_hw_watchdog=require, hw_watchdog=config,
    )
    fake = FakeClient()
    fake.holding_registers = dict(registers)
    fake.hr_raises = hr_raises
    d._client = fake  # noqa: SLF001
    ok = await d.connect()
    assert ok is True  # socket is up regardless of the watchdog verdict
    return d


async def test_hw_watchdog_armed_when_config_matches():
    """A correctly-armed ADAM (watchdog enabled, timeout in band, all DO safety
    values OFF) lets the driver arm and assert outputs.
    """
    d = await _connect_with_registers(_armed_registers())
    assert d.hw_watchdog_ok() is True
    ok, detail = d.hw_watchdog_status()
    assert ok is True
    assert "armed" in detail
    # And an assert is now allowed through.
    await d.drive_outputs(inhibit=True)
    assert (DO_COIL_BASE + DO_INHIBIT, True) in d._client.writes  # noqa: SLF001


async def test_hw_watchdog_disabled_refuses_assert():
    """Watchdog disabled on the unit → not armed → asserting an output raises
    and the status explains why (acceptance: a visible refusal, never silent arm).
    """
    regs = _armed_registers()
    regs[_WD_ENABLE_REG] = 0  # disabled
    d = await _connect_with_registers(regs)
    assert d.hw_watchdog_ok() is False
    _ok, detail = d.hw_watchdog_status()
    assert "not enabled" in detail
    with pytest.raises(HwWatchdogNotArmedError):
        await d.drive_outputs(inhibit=True)
    with pytest.raises(HwWatchdogNotArmedError):
        await d.drive_outputs(force_transfer=True)
    with pytest.raises(HwWatchdogNotArmedError):
        await d.drive_outputs(test_pulse_ms=750)


async def test_hw_watchdog_timeout_out_of_band_not_armed():
    regs = _armed_registers()
    regs[_WD_TIMEOUT_REG] = 200  # 20.0 s — longer than the software watchdog band
    d = await _connect_with_registers(regs)
    assert d.hw_watchdog_ok() is False
    assert "timeout" in d.hw_watchdog_status()[1]


async def test_hw_watchdog_nonzero_safety_value_not_armed():
    """If any DO safety value is ON, the relays would NOT de-energise on host
    loss — must refuse to arm.
    """
    regs = _armed_registers()
    regs[_WD_SAFETY_BASE + DO_INHIBIT] = 1  # Inhibit would stay latched
    d = await _connect_with_registers(regs)
    assert d.hw_watchdog_ok() is False
    assert "safety value" in d.hw_watchdog_status()[1]


async def test_hw_watchdog_unconfigured_addresses_fail_closed():
    """require_hw_watchdog=True but no register addresses configured → fail
    closed with an actionable message, never a silent arm.
    """
    d = await _connect_with_registers({}, config=None)
    assert d.hw_watchdog_ok() is False
    assert "not configured" in d.hw_watchdog_status()[1]
    with pytest.raises(HwWatchdogNotArmedError):
        await d.drive_outputs(inhibit=True)


async def test_hw_watchdog_read_error_fails_closed():
    """A Modbus error reading the watchdog registers must fail closed, not
    optimistically assume armed.
    """
    d = await _connect_with_registers(_armed_registers(), hr_raises=True)
    assert d.hw_watchdog_ok() is False
    assert "could not read" in d.hw_watchdog_status()[1]


async def test_hw_watchdog_waiver_arms_without_reading():
    """require_hw_watchdog=False (bench waiver) arms immediately and never reads
    the watchdog registers.
    """
    d = await _connect_with_registers({}, require=False, config=None)
    assert d.hw_watchdog_ok() is True
    assert "waived" in d.hw_watchdog_status()[1]
    # No holding-register read was attempted.
    assert not any(fn == "read_holding_registers" for fn, _ in d._client.reads)  # noqa: SLF001
    # And outputs may be asserted.
    await d.drive_outputs(force_transfer=True)
    assert (DO_COIL_BASE + DO_FORCE_TRANSFER, True) in d._client.writes  # noqa: SLF001


async def test_hw_watchdog_release_allowed_while_not_armed():
    """Even when not armed, a RELEASE (de-assert) must pass through — this is
    what lets the comms-loss safety watchdog and bench cleanup drop relays.
    """
    regs = _armed_registers()
    regs[_WD_ENABLE_REG] = 0  # not armed
    d = await _connect_with_registers(regs)
    assert d.hw_watchdog_ok() is False
    # Releases do not raise.
    await d.drive_outputs(inhibit=False, force_transfer=False)
    assert (DO_COIL_BASE + DO_INHIBIT, False) in d._client.writes  # noqa: SLF001
    assert (DO_COIL_BASE + DO_FORCE_TRANSFER, False) in d._client.writes  # noqa: SLF001


async def test_hw_watchdog_unverified_until_connect():
    """Before connect() runs the check, a required-but-unchecked driver is fail
    closed (not armed).
    """
    d = IOAdamDriver(
        host="127.0.0.1", port=5020,
        require_hw_watchdog=True, hw_watchdog=_armed_config(),
    )
    assert d.hw_watchdog_ok() is False  # not yet verified


async def test_hw_watchdog_rechecks_on_reconnect():
    """The check re-runs on every (re)connect, so an ADAM that loses its config
    (swap / factory reset) is caught when the link re-establishes.
    """
    d = await _connect_with_registers(_armed_registers())
    assert d.hw_watchdog_ok() is True
    # Simulate the unit coming back factory-reset (watchdog disabled) and the
    # driver reconnecting.
    d._client.holding_registers[_WD_ENABLE_REG] = 0  # noqa: SLF001
    await d.connect()
    assert d.hw_watchdog_ok() is False


# ─── CALIBRATION fault: impossible contact combination (ICD §5.1.1) ──────────


async def test_both_position_auxes_set_flags_calibration(driver):
    """Both position auxes (14AA on-normal + 14BA on-emergency) closed at once
    is physically impossible for a transfer switch — a welded or miswired aux.
    The snapshot must raise CALIBRATION (ICD §5.1.1) so GenWatch can tell a
    sensor fault from a normal mid-stroke, not just a bare position=unknown.
    """
    from atspi.io_driver import FAULT_CALIBRATION

    fake = driver._client  # noqa: SLF001
    fake.di_bits[DI_ON_NORMAL] = True
    fake.di_bits[DI_ON_EMERGENCY] = True
    snap = await driver.read_inputs()
    assert snap.position == "unknown"
    assert snap.fault_bits & FAULT_CALIBRATION


async def test_both_position_auxes_open_is_not_calibration(driver):
    """Both auxes open is a legitimate mid-stroke: position=unknown but NO
    CALIBRATION fault (the bit is reserved for the impossible both-closed case).
    """
    from atspi.io_driver import FAULT_CALIBRATION

    snap = await driver.read_inputs()  # all di_bits default False
    assert snap.position == "unknown"
    assert not (snap.fault_bits & FAULT_CALIBRATION)


async def test_single_position_aux_is_not_calibration(driver):
    """A normal single-source position carries no CALIBRATION bit."""
    from atspi.io_driver import FAULT_CALIBRATION

    fake = driver._client  # noqa: SLF001
    fake.di_bits[DI_ON_EMERGENCY] = True
    snap = await driver.read_inputs()
    assert snap.position == "generator"
    assert not (snap.fault_bits & FAULT_CALIBRATION)
