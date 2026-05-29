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

    async def connect(self) -> bool:
        self.connected = True
        return True

    def close(self) -> None:
        self.connected = False

    async def read_coils(self, address, count, slave):
        if address == DI_COIL_BASE:
            bits = list(self.di_bits[:count])
        elif address == DO_COIL_BASE:
            bits = list(self.do_bits[:count])
        else:
            bits = [False] * count
        # pymodbus rounds bits up to byte; replicate that quirk
        while len(bits) % 8 != 0:
            bits.append(False)
        return FakeResult(bits=bits)

    async def write_coil(self, address, value, slave):
        self.writes.append((address, value))
        idx = address - DO_COIL_BASE
        if 0 <= idx < len(self.do_bits):
            self.do_bits[idx] = value
        return FakeResult()


@pytest.fixture
def driver():
    d = IOAdamDriver(host="127.0.0.1", port=5020, unit_id=1)
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
