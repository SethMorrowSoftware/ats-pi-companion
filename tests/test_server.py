"""Server-layer tests — verify the data block routes reads and writes
through to the store, and dispatches command intents.
"""
from __future__ import annotations

from atspi.server import _make_data_block
from atspi.state import (
    ADDR_CMD_INHIBIT,
    ADDR_CMD_TEST,
    ADDR_UNIT_ID,
    CommandIntent,
    RegisterStore,
)


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
    store = RegisterStore()
    intents: list[CommandIntent] = []
    block = _make_data_block(store, on_read=None, on_command=intents.append)
    block.setValues(ADDR_CMD_INHIBIT + 1, [1])
    assert intents == [CommandIntent(inhibit=True)]


def test_set_values_does_not_dispatch_unrecognized_writes():
    store = RegisterStore()
    intents: list[CommandIntent] = []
    block = _make_data_block(store, on_read=None, on_command=intents.append)
    block.setValues(1, [0])  # writing to ADDR_POSITION isn't a command
    assert intents == []


def test_set_values_multiple_addresses():
    """Writing multiple registers in one PDU dispatches each recognized one."""
    store = RegisterStore()
    intents: list[CommandIntent] = []
    block = _make_data_block(store, on_read=None, on_command=intents.append)
    # ADDR_CMD_TEST=0x0100, ADDR_CMD_INHIBIT=0x0101
    block.setValues(ADDR_CMD_TEST + 1, [1, 1])
    assert len(intents) == 2
    assert intents[0].test_pulse_ms is not None
    assert intents[1].inhibit is True
