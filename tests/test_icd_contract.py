"""End-to-end contract tests against the ATS-Pi ↔ GenWatch ICD.

These spin up the real Modbus server (in-process) and drive it with a
real pymodbus client, asserting wire-level conformance to the spec at
``GenWatch/docs/integrations/ats-pi-icd.md``. They are the canonical
defence against contract drift on the ATS-Pi side; the complementary
"does the GenWatch consumer match the ICD" test must live in the
GenWatch repo.

Known deviations the ICD calls out as MUST but pymodbus 3.7 cannot
emit (documented in CHANGELOG):

  * Reserved-range write rejection returns Modbus exception 0x02
    (illegal data address) instead of 0x03 (illegal data value).
  * Mode-policy rejection returns Modbus exception 0x02 instead of
    0x04 (server device failure).

In both cases the safety property the ICD actually cares about — the
write is rejected with a Modbus exception, GenWatch knows — holds.
The exact code is a known deviation; both client-side workarounds
(treat any exception as rejection) are trivial.
"""
from __future__ import annotations

import asyncio

import pytest
from pymodbus.client import AsyncModbusTcpClient

from atspi.io_driver import InputSnapshot
from atspi.io_mock import IOMockDriver
from atspi.server import start_server
from atspi.state import (
    ADDR_ATS_MODE,
    ADDR_CMD_BYPASS_DELAY,
    ADDR_CMD_BYPASS_DELAY_RB,
    ADDR_CMD_FORCE_TRANSFER,
    ADDR_CMD_FORCE_TRANSFER_RB,
    ADDR_CMD_INHIBIT,
    ADDR_CMD_INHIBIT_RB,
    ADDR_CMD_TEST,
    ADDR_CMD_TEST_RB,
    ADDR_EMERGENCY_AVAIL,
    ADDR_ENGINE_START_CALLING,
    ADDR_FAULT_SUMMARY,
    ADDR_FW_MAJOR,
    ADDR_ICD_MAJOR,
    ADDR_LAST_RETRANSFER_TS,
    ADDR_LAST_TRANSFER_TS,
    ADDR_NORMAL_AVAIL,
    ADDR_POSITION,
    ADDR_TRANSFER_COUNT_24H,
    ADDR_TRANSFER_COUNT_LIFETIME,
    ADDR_UNIT_ID,
    ADDR_UPTIME_S,
    ADDR_WALLCLOCK,
    FAULT_INPUT,
    RegisterStore,
)

# ─── Test fixtures ────────────────────────────────────────────────────────


def _seed_auto(store: RegisterStore, position: str = "utility") -> None:
    """Seed AUTO mode so command writes pass the validate() gate."""
    store.apply_input_snapshot(InputSnapshot(
        position=position,
        normal_available=(position == "utility"),
        emergency_available=True,
        engine_start_calling=False,
        ats_mode="auto",
        fault_bits=0,
    ))


@pytest.fixture
async def server(unused_tcp_port):
    """Start a real pymodbus server with an AUTO-seeded store, plus a
    mock I/O driver and command-dispatch wiring that mirror the real
    ``__main__`` setup. Yield ``(client, store)``.

    A sampling task is NOT started — tests drive ``store`` directly
    when they want a specific state. The driver/dispatch path is
    populated so command writes actually fire and the read-back
    registers can be re-seeded from ``driver.read_output_state()``
    after a brief settling sleep.
    """
    store = RegisterStore(unit_id=23)
    _seed_auto(store)
    driver = IOMockDriver()
    await driver.connect()

    loop = asyncio.get_running_loop()
    dispatch_done = asyncio.Event()
    pending = 0

    async def dispatch(intent):
        await driver.drive_outputs(
            test_pulse_ms=intent.test_pulse_ms,
            inhibit=intent.inhibit,
            force_transfer=intent.force_transfer,
            bypass_delay_pulse_ms=intent.bypass_delay_pulse_ms,
        )
        # Re-sync the store's read-back registers from the driver so the
        # client's next read sees the new physical state.
        store.apply_output_state(await driver.read_output_state())
        nonlocal pending
        pending -= 1
        if pending == 0:
            dispatch_done.set()

    def on_command(intent):
        nonlocal pending
        pending += 1
        dispatch_done.clear()
        loop.create_task(dispatch(intent))

    # Background pulse-release sync: the mock driver self-clears pulsed
    # outputs after their duration; mirror that into the store so the
    # client's read of the read-back register reflects the release.
    async def pulse_sync():
        while True:
            await asyncio.sleep(0.05)
            store.apply_output_state(await driver.read_output_state())

    sync_task = asyncio.create_task(pulse_sync(), name="pulse-sync")

    task = await start_server(
        host="127.0.0.1", port=unused_tcp_port, unit_id=1, store=store,
        on_command=on_command,
    )
    client = AsyncModbusTcpClient(host="127.0.0.1", port=unused_tcp_port)
    await client.connect()
    try:
        yield client, store
    finally:
        client.close()
        sync_task.cancel()
        task.cancel()
        for t in (sync_task, task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await driver.close()


def _u32(words: list[int]) -> int:
    """ICD §4.1: u32 is big-endian word order, high word at lower address."""
    assert len(words) == 2
    return (words[0] << 16) | words[1]


# ─── §1.4 + §9: ICD version + identification ─────────────────────────────


async def test_icd_version_is_1_0(server):
    client, _ = server
    r = await client.read_holding_registers(address=ADDR_ICD_MAJOR, count=2, slave=1)
    assert not r.isError()
    assert r.registers == [1, 0], (
        f"ICD version drift: register {ADDR_ICD_MAJOR:#06x}/.+1 = {r.registers}; "
        "this implementation declares 1.0"
    )


async def test_unit_id_matches_configuration(server):
    client, _ = server
    r = await client.read_holding_registers(address=ADDR_UNIT_ID, count=1, slave=1)
    assert r.registers == [23]


async def test_firmware_version_reads_as_three_words(server):
    """ICD §1.4 — fw_{major,minor,patch} are u16 at 0x0032-0x0034."""
    client, _ = server
    r = await client.read_holding_registers(address=ADDR_FW_MAJOR, count=3, slave=1)
    assert not r.isError()
    # 0.1.0 at the time of this test; we don't pin the patch, just shape.
    major, minor, patch = r.registers
    assert isinstance(major, int) and 0 <= major < 65536
    assert isinstance(minor, int) and 0 <= minor < 65536
    assert isinstance(patch, int) and 0 <= patch < 65536


# ─── §1.1: core state encoding ────────────────────────────────────────────


@pytest.mark.parametrize("position,expected_value", [
    ("utility", 0), ("generator", 1), ("transferring", 2), ("unknown", 3),
])
async def test_position_enum_encoding(server, position, expected_value):
    client, store = server
    _seed_auto(store, position=position)
    r = await client.read_holding_registers(address=ADDR_POSITION, count=1, slave=1)
    assert r.registers == [expected_value]


async def test_core_state_prime_poll_returns_six_words(server):
    """ICD §1.1: GenWatch's prime poll reads 0x0000–0x0005 (6 words)."""
    client, _ = server
    r = await client.read_holding_registers(address=ADDR_POSITION, count=6, slave=1)
    assert not r.isError()
    assert len(r.registers) == 6
    # All six values must be valid per their encodings.
    assert r.registers[0] in (0, 1, 2, 3)               # position
    assert r.registers[1] in (0, 1)                     # normal_available
    assert r.registers[2] in (0, 1)                     # emergency_available
    assert r.registers[3] in (0, 1)                     # engine_start_calling
    assert r.registers[4] in (0, 1, 2, 3)               # ats_mode
    assert 0 <= r.registers[5] <= 0x000F                # fault_summary defined bits only


async def test_boolean_inputs_are_zero_or_one_exactly(server):
    """ICD §4.2: ATS-Pi MUST emit exactly 0x0000 or 0x0001 for booleans."""
    client, store = server
    _seed_auto(store)
    for addr in (ADDR_NORMAL_AVAIL, ADDR_EMERGENCY_AVAIL, ADDR_ENGINE_START_CALLING):
        r = await client.read_holding_registers(address=addr, count=1, slave=1)
        assert r.registers[0] in (0, 1), (
            f"register {addr:#06x} = {r.registers[0]}; must be exactly 0 or 1"
        )


@pytest.mark.parametrize("mode,expected", [
    ("auto", 0), ("manual", 1), ("test", 2), ("unknown", 3),
])
async def test_ats_mode_enum_encoding(server, mode, expected):
    client, store = server
    store.apply_input_snapshot(InputSnapshot(
        position="utility", normal_available=True, emergency_available=True,
        engine_start_calling=False, ats_mode=mode, fault_bits=0,
    ))
    r = await client.read_holding_registers(address=ADDR_ATS_MODE, count=1, slave=1)
    assert r.registers == [expected]


async def test_fault_summary_masks_reserved_bits(server):
    """ICD §1.1.1: bits 4-15 are RESERVED and MUST be 0 on the wire."""
    client, store = server
    # Inject a buggy driver snapshot that sets bit 7 (reserved).
    store.apply_input_snapshot(InputSnapshot(
        position="utility", normal_available=True, emergency_available=True,
        engine_start_calling=False, ats_mode="auto",
        fault_bits=0x0080 | 0x0008,  # reserved bit 7 + defined CALIBRATION
    ))
    r = await client.read_holding_registers(address=ADDR_FAULT_SUMMARY, count=1, slave=1)
    bits = r.registers[0]
    # Defined CALIBRATION bit survives; reserved bit 7 is masked off.
    assert bits & 0x0008
    assert bits & 0x0080 == 0, (
        f"fault_summary leaked reserved bit: {bits:#06x}"
    )


# ─── §4.1: u32 word order ─────────────────────────────────────────────────


async def test_u32_word_order_big_endian_high_word_low_address(server):
    """ICD §4.1: u32 high word at lower address (big-endian word order)."""
    client, store = server
    # Synthesize a transfer-to-generator at a known wallclock value via
    # the public sampling API: utility → generator transition sets
    # last_transfer_to_gen_ts to the current wallclock.
    import time
    real_time = time.time
    # Snapshot the wallclock RegisterStore will use, then transition.
    now = int(real_time())
    _seed_auto(store, position="utility")
    _seed_auto(store, position="generator")

    r = await client.read_holding_registers(address=ADDR_LAST_TRANSFER_TS, count=2, slave=1)
    assert not r.isError()
    decoded = _u32(r.registers)
    # Within ±5s of "now" — accounts for clock advancement during the test.
    assert abs(decoded - now) <= 5, (
        f"u32 word order broken: got {decoded:#010x} ({decoded}), "
        f"expected ≈ {now:#010x} ({now})"
    )


async def test_uptime_is_monotonic(server):
    """ICD §6.2: uptime_s strictly increasing within a boot."""
    client, _ = server
    r1 = await client.read_holding_registers(address=ADDR_UPTIME_S, count=2, slave=1)
    await asyncio.sleep(1.05)
    r2 = await client.read_holding_registers(address=ADDR_UPTIME_S, count=2, slave=1)
    assert _u32(r2.registers) >= _u32(r1.registers) + 1


async def test_uptime_survives_wallclock_jump_backward(server, monkeypatch):
    """ICD §6.2 + ICD §7.3: uptime_s MUST be monotonic within a boot —
    'Backward jump = undetected reboot' on GenWatch's side. An NTP
    correction backward (or any other wall-clock change) must not move
    uptime backward.
    """
    import time as time_mod
    client, _ = server
    r1 = await client.read_holding_registers(address=ADDR_UPTIME_S, count=2, slave=1)
    before = _u32(r1.registers)

    # Slam wall-clock 1 hour into the past while the service is running.
    real_time = time_mod.time
    monkeypatch.setattr(time_mod, "time", lambda: real_time() - 3600)

    r2 = await client.read_holding_registers(address=ADDR_UPTIME_S, count=2, slave=1)
    after = _u32(r2.registers)
    assert after >= before, (
        f"uptime_s went backward across wall-clock NTP correction: "
        f"{before} → {after}; ICD §6.2 says this is reserved for reboots"
    )


async def test_u32_word_pair_coherent_across_second_boundary(server):
    """Regression: a u32 read MUST return a coherent (high, low) pair
    even if the read happens to straddle a second boundary. Previously
    each word called time.time() separately, so a 1-second tick between
    the two calls produced an inconsistent value.
    """
    import time
    client, _ = server
    # Hammer the wallclock register across many ticks; spec says high and
    # low words must combine to give a value within a tight bound of "now".
    for _ in range(50):
        wall_before = int(time.time())
        r = await client.read_holding_registers(
            address=ADDR_WALLCLOCK, count=2, slave=1,
        )
        wall_after = int(time.time())
        reconstructed = _u32(r.registers)
        # The reconstructed value must lie within [before, after] +-1s.
        # Without the fix, an off-by-one-second straddle would put it
        # at wall_before + 0x10000 (high word advanced, low word old) or
        # similar gibberish.
        assert wall_before - 1 <= reconstructed <= wall_after + 1, (
            f"u32 wallclock read inconsistent: "
            f"reconstructed={reconstructed} not in "
            f"[{wall_before}, {wall_after}]"
        )


async def test_wallclock_is_present_and_recent(server):
    """ICD §1.2: wallclock returns ATS-Pi's epoch s at moment of read."""
    import time
    client, _ = server
    before = int(time.time())
    r = await client.read_holding_registers(address=ADDR_WALLCLOCK, count=2, slave=1)
    after = int(time.time())
    wc = _u32(r.registers)
    # Generous bounds for slow runners.
    assert before - 2 <= wc <= after + 2


# ─── §3: reserved-range read & write behaviour ────────────────────────────


@pytest.mark.parametrize("addr", [
    0x0006,  # ICD §1.1 reserved hole
    0x000F,  # top of §1.1 reserved hole
    0x0018,  # §1.2 reserved hole
    0x0024,  # §1.3 reserved hole
    0x0036,  # §1.4 reserved hole
    0x0044,  # §1.5 reserved hole
    0x0050,  # §3 generic reserved
    0x00FF,  # §3 top of generic reserved
    0x0104,  # §3 just past last command
    0x010F,  # §3 top of command-reserved
    0x1234,  # §3 way out
])
async def test_reserved_addresses_read_zero(server, addr):
    """ICD §3: reserved ranges MUST return 0x0000 on read."""
    client, _ = server
    r = await client.read_holding_registers(address=addr, count=1, slave=1)
    assert not r.isError(), f"reserved read at {addr:#06x} errored: {r}"
    assert r.registers == [0], f"reserved {addr:#06x} = {r.registers[0]}; must be 0"


@pytest.mark.parametrize("addr", [
    0x0050,  # generic reserved
    ADDR_POSITION,            # read-only state
    ADDR_FAULT_SUMMARY,       # read-only state
    ADDR_UNIT_ID,             # read-only identification
    0x0104,                   # just past last command register
    0x1234,                   # way out
])
async def test_writes_to_non_command_addresses_are_rejected(server, addr):
    """ICD §3: writes to reserved + read-only addresses MUST return a
    Modbus exception. (ICD prefers 0x03; pymodbus emits 0x02 — known
    deviation; the safety property "write rejected" holds.)
    """
    client, _ = server
    r = await client.write_register(address=addr, value=1, slave=1)
    assert r.isError(), f"write to {addr:#06x} succeeded; ICD requires rejection"


async def test_invalid_value_writes_are_acknowledged_and_ignored(server):
    """ICD §6: a write whose VALUE doesn't match a defined pattern MUST
    return Modbus exception 0x03. pymodbus 3.7's ``validate()`` hook sees
    only (function code, address, count) — never the value — so the server
    cannot emit that exception: the write is acknowledged on the wire and
    then dropped (no CommandIntent, no relay action, no read-back change).
    Known deviation #3 in CHANGELOG; this test pins the actual behaviour so
    an accidental change is caught. The safety property that matters — an
    out-of-pattern value never reaches a relay — is asserted here too.
    """
    client, _store = server
    # Maintained command with a bogus value.
    r1 = await client.write_register(address=ADDR_CMD_INHIBIT, value=5, slave=1)
    assert not r1.isError()  # deviation: ICD prefers exception 0x03
    # Pulsed command with the one value its pattern does NOT define.
    r2 = await client.write_register(address=ADDR_CMD_TEST, value=0, slave=1)
    assert not r2.isError()
    # Neither write may reach the driver: read-backs stay released well past
    # the 500 ms read-back-reflection window.
    await asyncio.sleep(0.6)
    rb_inhibit = await client.read_holding_registers(
        address=ADDR_CMD_INHIBIT_RB, count=1, slave=1)
    rb_test = await client.read_holding_registers(
        address=ADDR_CMD_TEST_RB, count=1, slave=1)
    assert rb_inhibit.registers == [0], "invalid value must not assert the relay"
    assert rb_test.registers == [0], "cmd_test=0 is undefined and must not pulse"


# ─── §2.1: command register write contract ───────────────────────────────


async def test_inhibit_assert_and_release_round_trip(server):
    """ICD §2.1: write 1 → read-back 1; write 0 → read-back 0."""
    client, _ = server
    await client.write_register(address=ADDR_CMD_INHIBIT, value=1, slave=1)
    # Read-back must reflect within 500 ms (ICD §2.1.1).
    await _poll_until(client, ADDR_CMD_INHIBIT_RB, expected=1, timeout_s=0.5)
    await client.write_register(address=ADDR_CMD_INHIBIT, value=0, slave=1)
    await _poll_until(client, ADDR_CMD_INHIBIT_RB, expected=0, timeout_s=0.5)


async def test_force_transfer_assert_and_release_round_trip(server):
    client, _ = server
    await client.write_register(address=ADDR_CMD_FORCE_TRANSFER, value=1, slave=1)
    await _poll_until(client, ADDR_CMD_FORCE_TRANSFER_RB, expected=1, timeout_s=0.5)
    await client.write_register(address=ADDR_CMD_FORCE_TRANSFER, value=0, slave=1)
    await _poll_until(client, ADDR_CMD_FORCE_TRANSFER_RB, expected=0, timeout_s=0.5)


async def test_test_pulse_self_clears(server):
    """ICD §2.1 + §11.5: test pulse asserts for ≥500 ms ≤1500 ms then clears."""
    client, _ = server
    await client.write_register(address=ADDR_CMD_TEST, value=1, slave=1)
    # The default pulse is 750 ms (middle of ICD range). Poll for assertion,
    # then for self-clear.
    await _poll_until(client, ADDR_CMD_TEST_RB, expected=1, timeout_s=0.5)
    await _poll_until(client, ADDR_CMD_TEST_RB, expected=0, timeout_s=2.0)


async def test_bypass_delay_pulse_self_clears(server):
    client, _ = server
    await client.write_register(address=ADDR_CMD_BYPASS_DELAY, value=1, slave=1)
    await _poll_until(client, ADDR_CMD_BYPASS_DELAY_RB, expected=1, timeout_s=0.5)
    await _poll_until(client, ADDR_CMD_BYPASS_DELAY_RB, expected=0, timeout_s=2.0)


# ─── §2.1 mode policy: ICD §11.4 ─────────────────────────────────────────


async def test_cmd_force_transfer_rejected_in_manual_mode(server):
    """ICD §11.4: force_transfer is auto-only. In manual, writes MUST be
    rejected (Modbus exception) AND fault_summary INPUT_FAULT MUST latch.
    """
    client, store = server
    store.apply_input_snapshot(InputSnapshot(
        position="utility", normal_available=True, emergency_available=True,
        engine_start_calling=False, ats_mode="manual", fault_bits=0,
    ))
    r = await client.write_register(address=ADDR_CMD_FORCE_TRANSFER, value=1, slave=1)
    assert r.isError(), "force_transfer in manual mode must be rejected"
    fault = await client.read_holding_registers(address=ADDR_FAULT_SUMMARY, count=1, slave=1)
    assert fault.registers[0] & FAULT_INPUT, (
        "fault_summary must latch INPUT_FAULT on mode-policy rejection "
        "(ICD §write response contract)"
    )
    # Read-back must remain 0 — physical relay must not have fired.
    rb = await client.read_holding_registers(address=ADDR_CMD_FORCE_TRANSFER_RB, count=1, slave=1)
    assert rb.registers == [0]


async def test_inhibit_allowed_in_manual_mode(server):
    """ICD §2.1: cmd_inhibit is allowed in {auto, manual}."""
    client, store = server
    store.apply_input_snapshot(InputSnapshot(
        position="utility", normal_available=True, emergency_available=True,
        engine_start_calling=False, ats_mode="manual", fault_bits=0,
    ))
    r = await client.write_register(address=ADDR_CMD_INHIBIT, value=1, slave=1)
    assert not r.isError(), "inhibit in manual mode must be accepted"


async def test_all_commands_rejected_when_mode_unknown(server):
    """Before the first sampling cycle, mode is 'unknown' — conservative
    policy is to reject everything until we've read a real mode.
    """
    from atspi.state import RegisterStore as RS
    client, _ = server
    # Forcibly clear the seeded mode by constructing a fresh server.
    fresh = RS()  # mode='unknown' before any apply_input_snapshot
    # Repoint the slave's store via _GuardedSlaveContext._store — but it's
    # easier to just directly test RegisterStore.can_write at this layer.
    for addr in (ADDR_CMD_TEST, ADDR_CMD_INHIBIT,
                 ADDR_CMD_FORCE_TRANSFER, ADDR_CMD_BYPASS_DELAY):
        assert fresh.can_write(addr) is False, (
            f"can_write({addr:#06x}) must be False when mode=unknown"
        )


# ─── §6.1: atomicity ──────────────────────────────────────────────────────


async def test_multi_word_read_returns_coherent_snapshot(server):
    """ICD §6.1: all 6 core-state words MUST come from a coherent
    snapshot, no torn reads.
    """
    import time
    client, store = server

    # Background task: flip the store back and forth while reads happen.
    stop_flipping = False

    async def flipper():
        i = 0
        while not stop_flipping:
            _seed_auto(store, position="utility" if i % 2 else "generator")
            i += 1
            await asyncio.sleep(0)  # yield without sleeping

    flip_task = asyncio.create_task(flipper())
    try:
        # Hammer reads. Each one must be internally consistent: if
        # position=utility (0) then normal_available should be 1.
        deadline = time.monotonic() + 0.5
        n = 0
        while time.monotonic() < deadline:
            r = await client.read_holding_registers(
                address=ADDR_POSITION, count=6, slave=1,
            )
            position, normal_avail = r.registers[0], r.registers[1]
            # _seed_auto sets normal_available = (position == 'utility').
            # A torn read would show position=0 with normal_avail=0 or
            # position=1 with normal_avail=1.
            expected_normal = 1 if position == 0 else 0
            assert normal_avail == expected_normal, (
                f"torn read after {n} samples: "
                f"position={position} normal_available={normal_avail}"
            )
            n += 1
    finally:
        stop_flipping = True
        await flip_task


# ─── §2.1.1: write response latency ──────────────────────────────────────


async def test_write_reply_arrives_within_100ms(server):
    """ICD §2.1.1: Modbus reply MUST be sent within 100 ms of write
    receipt, regardless of how long the physical relay takes.
    """
    import time
    client, _ = server
    t0 = time.perf_counter()
    r = await client.write_register(address=ADDR_CMD_INHIBIT, value=1, slave=1)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert not r.isError()
    # Generous bound — local loopback + asyncio scheduling well under 100 ms.
    assert elapsed_ms < 100, (
        f"write reply took {elapsed_ms:.1f} ms; ICD §2.1.1 requires < 100 ms"
    )


# ─── §10: golden sequence ────────────────────────────────────────────────


async def test_golden_sequence_utility_to_generator_and_back(server):
    """ICD §10: simulate the canonical transfer/retransfer story; assert
    position transitions, counters, timestamps behave as spec'd.
    """
    client, store = server

    # t=0 — Boot state seeded by fixture: utility, both sources healthy.
    r = await client.read_holding_registers(address=ADDR_POSITION, count=4, slave=1)
    assert r.registers[0] == 0  # position=utility
    assert r.registers[1] == 1  # normal_available

    # Lifetime counter starts at 0.
    r = await client.read_holding_registers(
        address=ADDR_TRANSFER_COUNT_LIFETIME, count=2, slave=1,
    )
    assert _u32(r.registers) == 0

    # Utility breaker opens; gen call starts.
    store.apply_input_snapshot(InputSnapshot(
        position="utility", normal_available=False, emergency_available=True,
        engine_start_calling=True, ats_mode="auto", fault_bits=0,
    ))
    r = await client.read_holding_registers(address=ADDR_POSITION, count=4, slave=1)
    assert r.registers == [0, 0, 1, 1]  # pos=utility, norm=0, emerg=1, start=1

    # Transferring intermediate.
    _seed_auto(store, position="transferring")
    r = await client.read_holding_registers(address=ADDR_POSITION, count=1, slave=1)
    assert r.registers == [2]

    # Settle on generator.
    store.apply_input_snapshot(InputSnapshot(
        position="generator", normal_available=False, emergency_available=True,
        engine_start_calling=True, ats_mode="auto", fault_bits=0,
    ))
    r = await client.read_holding_registers(address=ADDR_POSITION, count=4, slave=1)
    assert r.registers == [1, 0, 1, 1]  # pos=generator

    # Lifetime counter incremented; last_transfer_to_gen_ts is non-zero.
    r = await client.read_holding_registers(
        address=ADDR_TRANSFER_COUNT_LIFETIME, count=2, slave=1,
    )
    assert _u32(r.registers) == 1
    r = await client.read_holding_registers(
        address=ADDR_LAST_TRANSFER_TS, count=2, slave=1,
    )
    assert _u32(r.registers) > 0
    # 24h count tracks lifetime here (single transfer in this test).
    r = await client.read_holding_registers(
        address=ADDR_TRANSFER_COUNT_24H, count=2, slave=1,
    )
    assert _u32(r.registers) == 1

    # Utility comes back. Engine still running during retransfer delay.
    store.apply_input_snapshot(InputSnapshot(
        position="generator", normal_available=True, emergency_available=True,
        engine_start_calling=True, ats_mode="auto", fault_bits=0,
    ))
    r = await client.read_holding_registers(address=ADDR_POSITION, count=4, slave=1)
    assert r.registers == [1, 1, 1, 1]

    # Retransfer completes — position back to utility, last_retransfer_ts
    # gets stamped, lifetime counter does NOT advance (forward direction only).
    _seed_auto(store, position="utility")
    r = await client.read_holding_registers(
        address=ADDR_TRANSFER_COUNT_LIFETIME, count=2, slave=1,
    )
    assert _u32(r.registers) == 1, "retransfer must not increment lifetime"
    r = await client.read_holding_registers(
        address=ADDR_LAST_RETRANSFER_TS, count=2, slave=1,
    )
    assert _u32(r.registers) > 0


# ─── Helpers ──────────────────────────────────────────────────────────────


async def _poll_until(client, addr: int, *, expected: int, timeout_s: float) -> None:
    """Poll register ``addr`` until it equals ``expected`` or timeout.
    Replaces fixed sleeps that race the sampling loop on slow runners.
    """
    import time
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        r = await client.read_holding_registers(address=addr, count=1, slave=1)
        if not r.isError() and r.registers[0] == expected:
            return
        last = r.registers[0] if not r.isError() else f"err {r}"
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"register {addr:#06x} did not become {expected} within {timeout_s}s "
        f"(last seen: {last})"
    )
