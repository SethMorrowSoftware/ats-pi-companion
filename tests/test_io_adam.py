"""ADAM-6060 driver tests using a fake pymodbus client.

These verify the bit-decoding and channel-mapping logic without
needing real hardware. Bench verification of the ADAM register map
itself is documented in docs/SPEC.md §8 phase E.
"""
from __future__ import annotations

import asyncio
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

    # Wait for the release to fire (clamped to 500 ms).
    await asyncio.sleep(0.7)
    assert (DO_COIL_BASE + DO_TEST, False) in fake.writes


async def test_drive_outputs_bypass_pulse_self_clears(driver):
    fake = driver._client  # noqa: SLF001
    await driver.drive_outputs(bypass_delay_pulse_ms=500)
    assert (DO_COIL_BASE + DO_BYPASS_DELAY, True) in fake.writes
    await asyncio.sleep(0.7)
    assert (DO_COIL_BASE + DO_BYPASS_DELAY, False) in fake.writes


async def test_read_output_state_decodes_bits(driver):
    fake = driver._client  # noqa: SLF001
    fake.do_bits[DO_TEST] = True
    fake.do_bits[DO_INHIBIT] = True
    out = await driver.read_output_state()
    assert out.test_active is True
    assert out.inhibit_active is True
    assert out.force_transfer_active is False
    assert out.bypass_delay_active is False


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
