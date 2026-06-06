"""Server-layer tests — verify the data block routes reads and writes
through to the store, and dispatches command intents.
"""
from __future__ import annotations

import asyncio

import pytest

from atspi.io_driver import InputSnapshot
from atspi.server import _GuardedSlaveContext, _make_data_block, start_server
from atspi.state import (
    ADDR_CMD_BYPASS_DELAY,
    ADDR_CMD_FORCE_TRANSFER,
    ADDR_CMD_INHIBIT,
    ADDR_CMD_TEST,
    ADDR_FAULT_SUMMARY,
    ADDR_POSITION,
    ADDR_UNIT_ID,
    CommandIntent,
    RegisterStore,
)


def _store_in_auto() -> RegisterStore:
    """RegisterStore seeded with one AUTO-mode sampling cycle, so command
    writes are accepted (write_register rejects in "unknown" mode).
    """
    store = RegisterStore()
    store.apply_input_snapshot(InputSnapshot(
        position="utility", normal_available=True, emergency_available=True,
        engine_start_calling=False, ats_mode="auto", fault_bits=0,
    ))
    return store


def test_get_values_reads_through_to_store():
    store = RegisterStore(unit_id=42)
    block = _make_data_block(store, on_read=None, on_command=None)
    # pymodbus passes 1-based addresses; ADDR_UNIT_ID = 0x0035 (PDU)
    vals = block.getValues(ADDR_UNIT_ID + 1, count=1)
    assert vals == [42]


def test_get_values_fires_on_read_callback():
    store = RegisterStore()
    calls = []
    block = _make_data_block(store, on_read=lambda: calls.append(1), on_command=None)
    block.getValues(1, count=3)
    assert len(calls) == 1  # one call per getValues, regardless of count


def test_set_values_dispatches_command_intent_for_recognized_writes():
    store = _store_in_auto()
    intents: list[CommandIntent] = []
    block = _make_data_block(store, on_read=None, on_command=intents.append)
    block.setValues(ADDR_CMD_INHIBIT + 1, [1])
    assert intents == [CommandIntent(inhibit=True)]


def test_set_values_does_not_dispatch_unrecognized_writes():
    store = _store_in_auto()
    intents: list[CommandIntent] = []
    block = _make_data_block(store, on_read=None, on_command=intents.append)
    block.setValues(1, [0])  # writing to ADDR_POSITION isn't a command
    assert intents == []


def test_set_values_multiple_addresses():
    """Writing multiple registers in one PDU dispatches each recognized one."""
    store = _store_in_auto()
    intents: list[CommandIntent] = []
    block = _make_data_block(store, on_read=None, on_command=intents.append)
    # ADDR_CMD_TEST=0x0100, ADDR_CMD_INHIBIT=0x0101
    block.setValues(ADDR_CMD_TEST + 1, [1, 1])
    assert len(intents) == 2
    assert intents[0].test_pulse_ms is not None
    assert intents[1].inhibit is True


# ─── Write-address validation ────────────────────────────────────────────


def _make_guarded():
    store = RegisterStore()
    # Seed AUTO mode so command-register writes pass the mode gate; tests
    # that exercise mode policy directly target RegisterStore.can_write.
    store.apply_input_snapshot(InputSnapshot(
        position="utility", normal_available=True, emergency_available=True,
        engine_start_calling=False, ats_mode="auto", fault_bits=0,
    ))
    block = _make_data_block(store, on_read=None, on_command=None)
    return _GuardedSlaveContext(hr=block, ir=block, store=store)


@pytest.mark.parametrize(
    "address",
    [
        ADDR_CMD_TEST, ADDR_CMD_INHIBIT, ADDR_CMD_FORCE_TRANSFER, ADDR_CMD_BYPASS_DELAY,
    ],
)
def test_guard_allows_writes_to_command_registers(address):
    ctx = _make_guarded()
    # FC06 = write single register
    assert ctx.validate(0x06, address, 1) is True


@pytest.mark.parametrize(
    "address",
    [
        ADDR_POSITION,         # 0x0000 — read-only state
        ADDR_FAULT_SUMMARY,    # 0x0005 — read-only state
        ADDR_UNIT_ID,          # 0x0035 — read-only identification
        0x0050,                # reserved hole
        0x0080,                # reserved hole
        0x00FF,                # reserved hole
        0x0104,                # reserved (just past last command)
        0x010F,                # reserved (top of reserved-write band)
        0x1234,                # way out
    ],
)
def test_guard_rejects_writes_to_non_command_addresses(address):
    ctx = _make_guarded()
    assert ctx.validate(0x06, address, 1) is False


def test_guard_rejects_multi_register_write_that_straddles_reserved():
    """FC16 from 0x0103 with count=2 covers 0x0103 (allowed) + 0x0104 (reserved).
    Per ICD any reserved address in the write rejects the whole operation.
    """
    ctx = _make_guarded()
    # FC16 = write multiple registers
    assert ctx.validate(0x10, ADDR_CMD_BYPASS_DELAY, 2) is False


def test_guard_rejects_all_coil_writes():
    """FC05/FC15 target the coil space, which the ATS-Pi does not expose.
    Coil writes are rejected unconditionally regardless of address.
    """
    ctx = _make_guarded()
    assert ctx.validate(0x05, ADDR_CMD_TEST, 1) is False  # FC05 even at allowed-holding addr
    assert ctx.validate(0x05, 0x0000, 1) is False
    assert ctx.validate(0x0F, 0x0000, 4) is False         # FC15 = multi-coil


def test_guard_rejects_fc23_read_write_multiple():
    """FC23 (read/write multiple registers) is a write-capable function the
    ATS-Pi does not define. pymodbus validates its read-range and write-range
    under the same function code, so validate() can't gate the write-range on
    its own — it's rejected wholesale (even at a command address) rather than
    letting an FC23 write to read-only/reserved space fall through to the
    bounds-only default validate().
    """
    ctx = _make_guarded()
    assert ctx.validate(0x17, ADDR_CMD_INHIBIT, 1) is False  # command addr
    assert ctx.validate(0x17, ADDR_POSITION, 1) is False     # read-only
    assert ctx.validate(0x17, 0x0104, 1) is False            # reserved


def test_guard_does_not_block_reads():
    ctx = _make_guarded()
    # FC03 = read holding registers
    assert ctx.validate(0x03, ADDR_POSITION, 6) is True
    assert ctx.validate(0x03, 0x0050, 4) is True  # reads of reserved addresses ok


async def test_end_to_end_write_to_reserved_returns_exception(unused_tcp_port):
    """Real Modbus client → real server → verify reserved-address write
    triggers a Modbus exception response.
    """
    from pymodbus.client import AsyncModbusTcpClient

    store = RegisterStore(unit_id=23)
    # Seed AUTO mode so the cmd_inhibit write below passes the validate()
    # mode gate (otherwise it would correctly reject as a mode violation,
    # masking the reserved-address behaviour we're trying to exercise).
    store.apply_input_snapshot(InputSnapshot(
        position="utility", normal_available=True, emergency_available=True,
        engine_start_calling=False, ats_mode="auto", fault_bits=0,
    ))
    task = await start_server(
        host="127.0.0.1", port=unused_tcp_port, unit_id=1, store=store,
    )
    try:
        c = AsyncModbusTcpClient(host="127.0.0.1", port=unused_tcp_port)
        await c.connect()

        # Valid write — should succeed.
        ok = await c.write_register(address=ADDR_CMD_INHIBIT, value=1, slave=1)
        assert ok.isError() is False

        # Reserved-address write — must be rejected with a Modbus exception.
        bad = await c.write_register(address=0x0080, value=1, slave=1)
        assert bad.isError() is True

        # Write to a read-only register — also rejected.
        ro = await c.write_register(address=ADDR_POSITION, value=99, slave=1)
        assert ro.isError() is True

        c.close()
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def test_end_to_end_fc23_write_is_rejected(unused_tcp_port):
    """A real FC23 (read/write multiple registers) request that writes a
    read-only or reserved register must be rejected with a Modbus exception
    rather than silently accepted (ICD §6.1). FC23 is a write path separate
    from FC06/FC16, so it gets its own end-to-end guard.
    """
    from pymodbus.client import AsyncModbusTcpClient

    # Modbus exception 0x02 (illegal data address). A literal rather than a
    # pymodbus constant: validate()-driven rejections always map to 0x02, and
    # the named constant moved in the 3.8 datastore API rework this repo pins
    # below (see pyproject pymodbus<3.8).
    exc_illegal_address = 0x02

    store = RegisterStore(unit_id=23)
    store.apply_input_snapshot(InputSnapshot(
        position="utility", normal_available=True, emergency_available=True,
        engine_start_calling=False, ats_mode="auto", fault_bits=0,
    ))
    task = await start_server(
        host="127.0.0.1", port=unused_tcp_port, unit_id=1, store=store,
    )
    try:
        c = AsyncModbusTcpClient(host="127.0.0.1", port=unused_tcp_port)
        await c.connect()

        # FC23 writing a read-only register (position) — rejected with 0x02.
        ro = await c.readwrite_registers(
            read_address=ADDR_POSITION, read_count=1,
            write_address=ADDR_POSITION, values=[99], slave=1,
        )
        assert ro.isError() is True
        assert ro.exception_code == exc_illegal_address

        # FC23 writing a reserved register — also rejected.
        res = await c.readwrite_registers(
            read_address=ADDR_POSITION, read_count=1,
            write_address=0x0104, values=[1], slave=1,
        )
        assert res.isError() is True
        assert res.exception_code == exc_illegal_address

        c.close()
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
