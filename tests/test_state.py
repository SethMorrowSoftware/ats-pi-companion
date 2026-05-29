"""Register store tests — ICD register layout, transitions, persistence."""
from __future__ import annotations

import time

import pytest

from atspi.io_driver import InputSnapshot, OutputState
from atspi.persistence import StateFile
from atspi.state import (
    ADDR_CMD_BYPASS_DELAY,
    ADDR_CMD_FORCE_TRANSFER,
    ADDR_CMD_FORCE_TRANSFER_RB,
    ADDR_CMD_INHIBIT,
    ADDR_CMD_INHIBIT_RB,
    ADDR_CMD_TEST,
    ADDR_FAULT_SUMMARY,
    ADDR_LAST_TRANSFER_TS,
    ADDR_POSITION,
    ADDR_TRANSFER_COUNT_24H,
    ADDR_TRANSFER_COUNT_LIFETIME,
    FAULT_INPUT,
    FAULT_OUTPUT,
    CommandIntent,
    RegisterStore,
)


def _inputs(position="utility", **overrides) -> InputSnapshot:
    base = dict(
        position=position,
        normal_available=True,
        emergency_available=True,
        engine_start_calling=False,
        ats_mode="auto",
        fault_bits=0,
    )
    base.update(overrides)
    return InputSnapshot(**base)


def test_transfer_to_generator_increments_lifetime_counter():
    store = RegisterStore()
    store.apply_input_snapshot(_inputs(position="utility"))
    assert store.read_register(ADDR_TRANSFER_COUNT_LIFETIME + 1) == 0

    store.apply_input_snapshot(_inputs(position="generator"))
    assert store.read_register(ADDR_TRANSFER_COUNT_LIFETIME + 1) == 1
    assert store.read_register(ADDR_TRANSFER_COUNT_24H + 1) == 1


def test_consecutive_generator_reads_do_not_double_count():
    store = RegisterStore()
    store.apply_input_snapshot(_inputs(position="utility"))
    store.apply_input_snapshot(_inputs(position="generator"))
    store.apply_input_snapshot(_inputs(position="generator"))
    store.apply_input_snapshot(_inputs(position="generator"))
    assert store.read_register(ADDR_TRANSFER_COUNT_LIFETIME + 1) == 1


def test_retransfer_to_utility_updates_timestamp_not_counter():
    store = RegisterStore()
    store.apply_input_snapshot(_inputs(position="utility"))
    store.apply_input_snapshot(_inputs(position="generator"))
    store.apply_input_snapshot(_inputs(position="utility"))
    # Lifetime stays at 1; only forward direction counts.
    assert store.read_register(ADDR_TRANSFER_COUNT_LIFETIME + 1) == 1
    # The retransfer timestamp got updated (non-zero).
    low = store.read_register(ADDR_LAST_TRANSFER_TS + 1)
    assert low > 0


def test_reboot_while_on_generator_does_not_count_transfer():
    """Boot position defaults to 'unknown'. The first read landing on
    'generator' (a reboot during a utility outage) must NOT count as a new
    transfer — otherwise the persisted lifetime count drifts up on every
    restart while the ATS sits on the generator.
    """
    store = RegisterStore()
    store.apply_input_snapshot(_inputs(position="generator", normal_available=False))
    assert store.read_register(ADDR_TRANSFER_COUNT_LIFETIME + 1) == 0
    assert store.read_register(ADDR_TRANSFER_COUNT_24H + 1) == 0


def test_position_glitch_through_unknown_does_not_double_count():
    """A momentary both-aux-open glitch reads as 'unknown'. Bouncing back to
    the same rail must not register a phantom transfer.
    """
    store = RegisterStore()
    store.apply_input_snapshot(_inputs(position="utility"))
    store.apply_input_snapshot(_inputs(position="generator"))  # real transfer
    assert store.read_register(ADDR_TRANSFER_COUNT_LIFETIME + 1) == 1
    store.apply_input_snapshot(_inputs(position="unknown"))     # glitch
    store.apply_input_snapshot(_inputs(position="generator"))   # bounce back
    assert store.read_register(ADDR_TRANSFER_COUNT_LIFETIME + 1) == 1


def test_transfer_through_transferring_counts_once():
    """The realistic utility → transferring → generator stroke counts exactly
    one transfer (the position seen just before 'generator' is 'transferring').
    """
    store = RegisterStore()
    store.apply_input_snapshot(_inputs(position="utility"))
    store.apply_input_snapshot(_inputs(position="transferring"))
    store.apply_input_snapshot(_inputs(position="generator"))
    assert store.read_register(ADDR_TRANSFER_COUNT_LIFETIME + 1) == 1


def test_retransfer_through_transferring_stamps_timestamp():
    """On real hardware a retransfer is generator → transferring → utility
    (the Load Disconnect pulse holds 'transferring' for ~2 s). The retransfer
    timestamp MUST still be stamped even though the position seen just before
    'utility' is 'transferring', not 'generator'.
    """
    from atspi.state import ADDR_LAST_RETRANSFER_TS
    store = RegisterStore()
    store.apply_input_snapshot(_inputs(position="utility"))
    store.apply_input_snapshot(_inputs(position="generator"))
    store.apply_input_snapshot(_inputs(position="transferring"))
    store.apply_input_snapshot(_inputs(position="utility"))
    hi = store.read_register(ADDR_LAST_RETRANSFER_TS)
    lo = store.read_register(ADDR_LAST_RETRANSFER_TS + 1)
    assert ((hi << 16) | lo) > 0, (
        "retransfer via 'transferring' must stamp last_retransfer_to_util_ts"
    )
    # And it must not be miscounted as a forward transfer.
    assert store.read_register(ADDR_TRANSFER_COUNT_LIFETIME + 1) == 1


def test_24h_count_evicts_old_entries(monkeypatch):
    """Transfers older than 24h drop out of the rolling counter."""
    fake_wall = [1_700_000_000]
    monkeypatch.setattr(time, "time", lambda: fake_wall[0])

    store = RegisterStore()
    # Initial transfer at t=1_700_000_000
    store.apply_input_snapshot(_inputs(position="utility"))
    store.apply_input_snapshot(_inputs(position="generator"))
    assert store.read_register(ADDR_TRANSFER_COUNT_24H + 1) == 1

    # 25 hours later, another transfer
    fake_wall[0] += 25 * 3600
    store.apply_input_snapshot(_inputs(position="utility"))
    store.apply_input_snapshot(_inputs(position="generator"))

    # The first one has aged out.
    assert store.read_register(ADDR_TRANSFER_COUNT_24H + 1) == 1
    # Lifetime keeps counting forever.
    assert store.read_register(ADDR_TRANSFER_COUNT_LIFETIME + 1) == 2


def test_24h_count_survives_restart_within_window(tmp_path, monkeypatch):
    """B7: the 24h sliding-window timestamps persist across restarts so
    a service restart for an unrelated reason doesn't zero the counter.
    """
    sf = StateFile(tmp_path / "state.json")
    fake_wall = [1_700_000_000]
    monkeypatch.setattr(time, "time", lambda: fake_wall[0])

    s1 = RegisterStore(state_file=sf)
    # Two transfers within the window.
    s1.apply_input_snapshot(_inputs(position="utility"))
    s1.apply_input_snapshot(_inputs(position="generator"))
    fake_wall[0] += 60
    s1.apply_input_snapshot(_inputs(position="utility"))
    s1.apply_input_snapshot(_inputs(position="generator"))
    assert s1.read_register(ADDR_TRANSFER_COUNT_24H + 1) == 2

    # Simulate restart 1 hour later.
    fake_wall[0] += 3600
    s2 = RegisterStore(state_file=sf)
    assert s2.read_register(ADDR_TRANSFER_COUNT_24H + 1) == 2
    assert s2.read_register(ADDR_TRANSFER_COUNT_LIFETIME + 1) == 2


def test_24h_count_evicts_stale_entries_at_startup(tmp_path, monkeypatch):
    """If a service is offline >24h, stale entries in the persisted
    window must be filtered out at load time.
    """
    sf = StateFile(tmp_path / "state.json")
    fake_wall = [1_700_000_000]
    monkeypatch.setattr(time, "time", lambda: fake_wall[0])

    s1 = RegisterStore(state_file=sf)
    s1.apply_input_snapshot(_inputs(position="utility"))
    s1.apply_input_snapshot(_inputs(position="generator"))
    assert s1.read_register(ADDR_TRANSFER_COUNT_24H + 1) == 1

    # Service down for 30 hours.
    fake_wall[0] += 30 * 3600
    s2 = RegisterStore(state_file=sf)
    # All persisted entries are now stale → 24h count drops to zero.
    assert s2.read_register(ADDR_TRANSFER_COUNT_24H + 1) == 0
    # Lifetime still preserved.
    assert s2.read_register(ADDR_TRANSFER_COUNT_LIFETIME + 1) == 1


def _store_in_auto() -> RegisterStore:
    """RegisterStore seeded with one AUTO-mode sampling cycle, so command
    writes are accepted (write_register rejects in "unknown" mode).
    """
    store = RegisterStore()
    store.apply_input_snapshot(_inputs(position="utility"))
    return store


def test_write_register_returns_command_intent():
    store = _store_in_auto()
    assert store.write_register(ADDR_CMD_TEST, 1) == CommandIntent(test_pulse_ms=750)
    assert store.write_register(ADDR_CMD_INHIBIT, 1) == CommandIntent(inhibit=True)
    assert store.write_register(ADDR_CMD_INHIBIT, 0) == CommandIntent(inhibit=False)
    assert store.write_register(ADDR_CMD_FORCE_TRANSFER, 1) == CommandIntent(force_transfer=True)
    assert store.write_register(ADDR_CMD_BYPASS_DELAY, 1) == CommandIntent(bypass_delay_pulse_ms=750)


def test_write_register_rejects_unknown_addresses_and_values():
    store = _store_in_auto()
    assert store.write_register(0x0FFF, 1) is None  # unknown address
    assert store.write_register(ADDR_CMD_TEST, 0) is None  # ICD: 1 to trigger
    assert store.write_register(ADDR_CMD_INHIBIT, 99) is None  # out-of-range


def test_write_register_does_not_mutate_readback():
    """Read-back state must reflect physical state, not the write."""
    store = _store_in_auto()
    intent = store.write_register(ADDR_CMD_INHIBIT, 1)
    assert intent is not None
    # No mutation yet — read-back stays 0 until the sampling loop sees
    # the driven output.
    assert store.read_register(ADDR_CMD_INHIBIT_RB) == 0

    # Sampling loop reports the driver has actually asserted it.
    store.apply_output_state(OutputState(
        test_active=False,
        inhibit_active=True,
        force_transfer_active=False,
        bypass_delay_active=False,
    ))
    assert store.read_register(ADDR_CMD_INHIBIT_RB) == 1


def test_release_maintained_commands_clears_inhibit_and_force_only():
    store = RegisterStore()
    store.apply_output_state(OutputState(
        test_active=True,
        inhibit_active=True,
        force_transfer_active=True,
        bypass_delay_active=True,
    ))
    store.release_maintained_commands()
    assert store.read_register(ADDR_CMD_INHIBIT_RB) == 0
    assert store.read_register(ADDR_CMD_FORCE_TRANSFER_RB) == 0


def test_input_and_output_fault_bits():
    store = RegisterStore()
    store.set_input_fault(True)
    assert store.read_register(ADDR_FAULT_SUMMARY) & FAULT_INPUT
    store.set_output_fault(True)
    bits = store.read_register(ADDR_FAULT_SUMMARY)
    assert bits & FAULT_INPUT and bits & FAULT_OUTPUT
    store.set_input_fault(False)
    bits = store.read_register(ADDR_FAULT_SUMMARY)
    assert not bits & FAULT_INPUT and bits & FAULT_OUTPUT


def test_output_fault_survives_successful_sampling_cycle():
    """A failed command sets OUTPUT_FAULT. The next successful read must NOT
    clear it (only an explicit set_output_fault(False) should).
    """
    store = RegisterStore()
    store.set_output_fault(True)
    assert store.read_register(ADDR_FAULT_SUMMARY) & FAULT_OUTPUT

    # Sampling loop applies a healthy snapshot (driver returns fault_bits=0).
    store.apply_input_snapshot(_inputs(position="utility"))

    assert store.read_register(ADDR_FAULT_SUMMARY) & FAULT_OUTPUT, (
        "OUTPUT_FAULT must persist across successful input reads"
    )


def test_apply_input_snapshot_preserves_set_input_fault():
    """set_input_fault(True) followed by apply_input_snapshot must not be
    overwritten by the driver's reported fault_bits=0.
    """
    store = RegisterStore()
    store.set_input_fault(True)
    store.apply_input_snapshot(_inputs(position="utility"))
    assert store.read_register(ADDR_FAULT_SUMMARY) & FAULT_INPUT


def test_driver_fault_bits_merge_with_local_bits():
    """Driver-reported bits (MODE_UNKNOWN, CALIBRATION) merge with locally-
    managed FAULT_INPUT / FAULT_OUTPUT.
    """
    from atspi.state import FAULT_CALIBRATION, FAULT_MODE_UNKNOWN
    store = RegisterStore()
    store.set_output_fault(True)
    store.apply_input_snapshot(
        _inputs(position="utility", fault_bits=FAULT_MODE_UNKNOWN | FAULT_CALIBRATION)
    )
    bits = store.read_register(ADDR_FAULT_SUMMARY)
    assert bits & FAULT_OUTPUT
    assert bits & FAULT_MODE_UNKNOWN
    assert bits & FAULT_CALIBRATION


def test_driver_cannot_set_local_fault_bits():
    """The driver's reported fault_bits must not be able to set/clear the
    bits the orchestrator owns. If a driver buggily reports FAULT_INPUT,
    we ignore that bit from the driver (orchestrator decides).
    """
    store = RegisterStore()
    # Buggy driver reports FAULT_INPUT in its snapshot. Orchestrator state
    # says no input fault. Result: FAULT_INPUT stays 0.
    store.apply_input_snapshot(_inputs(position="utility", fault_bits=FAULT_INPUT))
    assert not (store.read_register(ADDR_FAULT_SUMMARY) & FAULT_INPUT)


def test_position_encoding():
    store = RegisterStore()
    for position, expected in [
        ("utility", 0), ("generator", 1), ("transferring", 2), ("unknown", 3),
    ]:
        store.apply_input_snapshot(_inputs(position=position))
        assert store.read_register(ADDR_POSITION) == expected


def test_transfer_count_persists_across_restarts(tmp_path):
    sf = StateFile(tmp_path / "state.json")

    s1 = RegisterStore(state_file=sf)
    s1.apply_input_snapshot(_inputs(position="utility"))
    s1.apply_input_snapshot(_inputs(position="generator"))

    s2 = RegisterStore(state_file=sf)
    assert s2.read_register(ADDR_TRANSFER_COUNT_LIFETIME + 1) == 1


def test_persistence_load_failure_does_not_crash(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    p.write_text("{corrupt")
    sf = StateFile(p)
    # Should fall back to zeros without raising.
    store = RegisterStore(state_file=sf)
    assert store.read_register(ADDR_TRANSFER_COUNT_LIFETIME + 1) == 0


def test_persistence_save_failure_does_not_crash(tmp_path):
    sf = StateFile(tmp_path / "state.json")
    store = RegisterStore(state_file=sf)
    # Replace the StateFile save method with a failing one.
    def boom(_):
        raise OSError("disk full")
    sf.save = boom  # type: ignore[assignment]
    # Triggering a transfer must not crash the store.
    store.apply_input_snapshot(_inputs(position="utility"))
    store.apply_input_snapshot(_inputs(position="generator"))
    assert store.read_register(ADDR_TRANSFER_COUNT_LIFETIME + 1) == 1


@pytest.mark.parametrize("addr", [0x0050, 0x00FF, 0x0200, 0x1234])
def test_reserved_addresses_read_zero(addr):
    store = RegisterStore()
    assert store.read_register(addr) == 0


# ─── Time-source correctness (regression for ICD §6.2 + u32 race) ────────


def test_uptime_uses_monotonic_not_wallclock(monkeypatch):
    """ICD §6.2: uptime_s MUST be strictly increasing within a boot. A
    backward wall-clock jump (NTP correction, manual clock set) must
    NOT move uptime backward — that signal is reserved for reboots.
    """
    # Build a store, sleep briefly, slam wall-clock backward, read uptime.
    store = RegisterStore()
    import time as time_mod
    # Pretend several seconds elapsed in monotonic time without changing
    # wall-clock. Then move wall-clock backward by an hour.
    real_mono = time_mod.monotonic
    monkeypatch.setattr(time_mod, "monotonic", lambda: real_mono() + 10)
    real_wall = time_mod.time
    monkeypatch.setattr(time_mod, "time", lambda: real_wall() - 3600)
    uptime_lo = store.read_register(ADDR_LAST_TRANSFER_TS)  # any uptime call
    # uptime in seconds; we set monotonic +10 vs boot mono, so uptime ≈ 10.
    from atspi.state import ADDR_UPTIME_S
    hi = store.read_register(ADDR_UPTIME_S)
    lo = store.read_register(ADDR_UPTIME_S + 1)
    uptime = (hi << 16) | lo
    assert uptime >= 10, (
        f"uptime={uptime}; should be >=10 (monotonic source) regardless of "
        "the wall-clock slam backward"
    )
    _ = uptime_lo  # silence unused


def test_read_register_u32_pinned_time_returns_coherent_pair():
    """When ``read_register`` is called with a pinned ``now_wall`` for both
    words of a u32, the high/low pair MUST reconstruct that exact value.
    Pre-fix, the two halves each called ``time.time()`` independently and
    could straddle a high-word boundary (every 65 536 s of wall-clock).
    """
    from atspi.state import ADDR_UPTIME_S, ADDR_WALLCLOCK

    store = RegisterStore()
    # A value that exercises both halves — pretend the wallclock is just
    # past the 0x10000 (65 536 s) boundary so the high word is non-zero.
    pinned_wall = 0x12345678
    pinned_mono = 1_000_000.0  # arbitrary fixed monotonic baseline
    hi = store.read_register(ADDR_WALLCLOCK, now_wall=pinned_wall, now_mono=pinned_mono)
    lo = store.read_register(ADDR_WALLCLOCK + 1, now_wall=pinned_wall, now_mono=pinned_mono)
    assert (hi << 16) | lo == pinned_wall, (
        f"u32 read returned inconsistent pair: ({hi:#06x}, {lo:#06x}) "
        f"→ {(hi << 16) | lo:#010x}, expected {pinned_wall:#010x}"
    )

    # Same for uptime — derived from now_mono.
    hi = store.read_register(ADDR_UPTIME_S, now_wall=pinned_wall, now_mono=pinned_mono)
    lo = store.read_register(ADDR_UPTIME_S + 1, now_wall=pinned_wall, now_mono=pinned_mono)
    uptime = (hi << 16) | lo
    # uptime = int(pinned_mono - boot_mono). boot_mono is set at __init__
    # to a real monotonic() value; pinned_mono is 1e6. The result has to
    # be self-consistent.
    expected = int(pinned_mono - store._boot_mono)  # noqa: SLF001
    assert uptime == expected & 0xFFFFFFFF


# ─── Mode enforcement (ICD §6) ───────────────────────────────────────────


def _seed_mode(store: RegisterStore, mode: str) -> None:
    store.apply_input_snapshot(_inputs(position="utility", ats_mode=mode))


@pytest.mark.parametrize(
    "addr,value",
    [
        (ADDR_CMD_TEST, 1),
        (ADDR_CMD_INHIBIT, 1),
        (ADDR_CMD_INHIBIT, 0),
        (ADDR_CMD_FORCE_TRANSFER, 1),
        (ADDR_CMD_FORCE_TRANSFER, 0),
        (ADDR_CMD_BYPASS_DELAY, 1),
    ],
)
def test_all_commands_accepted_in_auto_mode(addr, value):
    store = RegisterStore()
    _seed_mode(store, "auto")
    assert store.write_register(addr, value) is not None


@pytest.mark.parametrize("value", [0, 1])
def test_inhibit_accepted_in_manual_mode(value):
    store = RegisterStore()
    _seed_mode(store, "manual")
    assert store.write_register(ADDR_CMD_INHIBIT, value) is not None


@pytest.mark.parametrize(
    "addr",
    [ADDR_CMD_TEST, ADDR_CMD_FORCE_TRANSFER, ADDR_CMD_BYPASS_DELAY],
)
def test_auto_only_commands_rejected_in_manual_mode(addr):
    store = RegisterStore()
    _seed_mode(store, "manual")
    assert store.write_register(addr, 1) is None


@pytest.mark.parametrize(
    "addr",
    [ADDR_CMD_TEST, ADDR_CMD_INHIBIT, ADDR_CMD_FORCE_TRANSFER, ADDR_CMD_BYPASS_DELAY],
)
def test_all_commands_rejected_in_test_or_unknown_mode(addr):
    for mode in ("test", "unknown"):
        store = RegisterStore()
        _seed_mode(store, mode)
        assert store.write_register(addr, 1) is None, (
            f"{addr:#06x} in mode={mode!r} should be rejected"
        )


def test_mode_reject_latches_fault_input_in_summary():
    """A mode-rejected write must set FAULT_INPUT in fault_summary, and
    the bit stays set until the next valid command clears it.
    """
    store = RegisterStore()
    _seed_mode(store, "manual")
    # cmd_test is auto-only — rejected in manual.
    assert store.write_register(ADDR_CMD_TEST, 1) is None
    assert store.read_register(ADDR_FAULT_SUMMARY) & FAULT_INPUT, (
        "Mode-rejected write must surface as FAULT_INPUT"
    )

    # Switching to auto alone must NOT clear the latch — the ICD says
    # "until next valid command clears it".
    _seed_mode(store, "auto")
    assert store.read_register(ADDR_FAULT_SUMMARY) & FAULT_INPUT

    # A valid command clears the latch.
    assert store.write_register(ADDR_CMD_INHIBIT, 1) is not None
    assert not (store.read_register(ADDR_FAULT_SUMMARY) & FAULT_INPUT)


def test_mode_reject_does_not_dispatch_command():
    """A mode-rejected write must NOT return a CommandIntent — the I/O
    driver must never see the physical command.
    """
    store = RegisterStore()
    _seed_mode(store, "manual")
    assert store.write_register(ADDR_CMD_FORCE_TRANSFER, 1) is None


def test_invalid_value_does_not_set_mode_reject_latch():
    """Value-invalid writes are a separate failure mode from
    mode-restricted writes — they must not latch FAULT_INPUT.
    """
    store = RegisterStore()
    _seed_mode(store, "auto")
    assert store.write_register(ADDR_CMD_INHIBIT, 99) is None
    assert not (store.read_register(ADDR_FAULT_SUMMARY) & FAULT_INPUT)


def test_default_unknown_mode_rejects_writes_until_first_sample():
    """Before the first sampling cycle, ats_mode is 'unknown'. All command
    writes are conservatively rejected — we shouldn't drive relays based
    on a mode we haven't read yet.
    """
    store = RegisterStore()
    # No apply_input_snapshot yet — mode is "unknown".
    assert store.write_register(ADDR_CMD_INHIBIT, 1) is None
    assert store.write_register(ADDR_CMD_TEST, 1) is None
