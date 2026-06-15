"""Hybrid driver tests: ASCO Group 5 serial reader (inputs) composed with an
ADAM-6060 driver (outputs + F1 safety, delegated).

Like test_io_adam.py these use fake pymodbus clients — they verify the
register/bit decode and the input/output split, not real hardware. The exact
Group 5 register map is bench-verified per docs/HARDWARE.md §3.1.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from atspi.io_adam import (
    DO_COIL_BASE,
    DO_INHIBIT,
    HwWatchdogNotArmedError,
    IOAdamDriver,
)
from atspi.io_asco_serial import AscoSerialConfig, AscoSerialReader, _extract_bit
from atspi.io_hybrid import IOHybridDriver

# Status-word bit layout used across these tests (arbitrary but fully configured).
_STATUS_REG = 0x0010
_BITS = dict(
    on_normal_bit=0,
    on_emergency_bit=1,
    normal_available_bit=2,
    emergency_available_bit=3,
    transferring_bit=4,
    engine_start_bit=5,
)


@dataclass
class FakeResult:
    registers: list[int] = field(default_factory=list)
    bits: list[bool] = field(default_factory=list)
    is_err: bool = False

    def isError(self) -> bool:  # noqa: N802 (pymodbus interface)
        return self.is_err


class FakeSerialClient:
    """Stand-in for AsyncModbusSerialClient: serves canned holding registers."""

    def __init__(self, connect_result: bool = True):
        self.connected = False
        self.connect_result = connect_result
        self.registers: dict[int, int] = {}
        self.raise_on_read = False
        self.reads: list[tuple[int, int]] = []

    async def connect(self) -> bool:
        self.connected = self.connect_result
        return self.connect_result

    def close(self) -> None:
        self.connected = False

    async def read_holding_registers(self, address, count, slave):
        self.reads.append((address, count))
        if self.raise_on_read:
            from pymodbus.exceptions import ModbusIOException
            raise ModbusIOException("simulated serial read failure")
        regs = [self.registers.get(address + i, 0) for i in range(count)]
        return FakeResult(registers=regs)


class FakeAdamClient:
    """Minimal stand-in for AsyncModbusTcpClient for the output side."""

    def __init__(self):
        self.connected = False
        self.do_bits = [False] * 6
        self.writes: list[tuple[int, bool]] = []

    async def connect(self) -> bool:
        self.connected = True
        return True

    def close(self) -> None:
        self.connected = False

    async def read_coils(self, address, count, slave):
        bits = list(self.do_bits[:count])
        while len(bits) % 8 != 0:
            bits.append(False)
        return FakeResult(bits=bits)

    async def write_coil(self, address, value, slave):
        self.writes.append((address, value))
        idx = address - DO_COIL_BASE
        if 0 <= idx < len(self.do_bits):
            self.do_bits[idx] = value
        return FakeResult()


def _cfg(**overrides) -> AscoSerialConfig:
    base = dict(status_register=_STATUS_REG, status_register_count=1, **_BITS)
    base.update(overrides)
    return AscoSerialConfig(**base)


def _reader(client: FakeSerialClient, **overrides) -> AscoSerialReader:
    r = AscoSerialReader(_cfg(**overrides))
    r._client = client  # noqa: SLF001
    r._connected = True  # noqa: SLF001
    return r


def _word(*bits: int) -> int:
    """Build a 16-bit status word from the given set bit positions."""
    v = 0
    for b in bits:
        v |= 1 << b
    return v


# ─── config validation (fail-fast at build time) ─────────────────────────


def test_unconfigured_config_refuses_to_build():
    # status_register + the four required bits unset → driver must not start.
    with pytest.raises(ValueError, match="status_register"):
        AscoSerialReader(AscoSerialConfig())


def test_missing_one_required_bit_refuses_to_build():
    with pytest.raises(ValueError, match="emergency_available_bit"):
        AscoSerialReader(
            AscoSerialConfig(
                status_register=_STATUS_REG,
                on_normal_bit=0,
                on_emergency_bit=1,
                normal_available_bit=2,
                # emergency_available_bit omitted
            )
        )


def test_bit_index_outside_read_block_rejected():
    # count=1 → only bits 0..15 are readable; bit 16 is out of range.
    with pytest.raises(ValueError, match="outside"):
        AscoSerialReader(_cfg(on_emergency_bit=16))


def test_invalid_assumed_mode_rejected():
    with pytest.raises(ValueError, match="assumed_mode"):
        AscoSerialReader(_cfg(assumed_mode="bogus"))


# ─── input decode / position derivation ──────────────────────────────────


async def test_position_utility():
    fake = FakeSerialClient()
    fake.registers[_STATUS_REG] = _word(_BITS["on_normal_bit"],
                                         _BITS["normal_available_bit"],
                                         _BITS["emergency_available_bit"])
    snap = await _reader(fake).read_inputs()
    assert snap.position == "utility"
    assert snap.normal_available is True
    assert snap.emergency_available is True
    assert snap.engine_start_calling is False


async def test_position_generator_with_engine_start():
    fake = FakeSerialClient()
    fake.registers[_STATUS_REG] = _word(_BITS["on_emergency_bit"],
                                        _BITS["emergency_available_bit"],
                                        _BITS["engine_start_bit"])
    snap = await _reader(fake).read_inputs()
    assert snap.position == "generator"
    assert snap.normal_available is False
    assert snap.emergency_available is True
    assert snap.engine_start_calling is True


async def test_transferring_bit_takes_precedence():
    fake = FakeSerialClient()
    # Transfer bit set even though on_normal is also set → transferring wins.
    fake.registers[_STATUS_REG] = _word(_BITS["on_normal_bit"], _BITS["transferring_bit"])
    snap = await _reader(fake).read_inputs()
    assert snap.position == "transferring"


async def test_position_unknown_when_both_sources():
    fake = FakeSerialClient()
    fake.registers[_STATUS_REG] = _word(_BITS["on_normal_bit"], _BITS["on_emergency_bit"])
    snap = await _reader(fake).read_inputs()
    assert snap.position == "unknown"


async def test_position_unknown_when_neither_source():
    fake = FakeSerialClient()
    fake.registers[_STATUS_REG] = 0
    snap = await _reader(fake).read_inputs()
    assert snap.position == "unknown"


async def test_multi_register_block_flat_bit_addressing():
    # count=2 → bit 18 is register (status+1), bit 2.
    fake = FakeSerialClient()
    fake.registers[_STATUS_REG] = _word(_BITS["on_emergency_bit"])
    fake.registers[_STATUS_REG + 1] = _word(2)  # flat bit 16+2 = 18
    snap = await _reader(
        fake, status_register_count=2, emergency_available_bit=18
    ).read_inputs()
    assert snap.position == "generator"
    assert snap.emergency_available is True
    # Confirm it actually read the 2-register block.
    assert (_STATUS_REG, 2) in fake.reads


async def test_optional_bits_absent_default_safe():
    # No transferring_bit / engine_start_bit configured → never transferring,
    # engine_start always False.
    fake = FakeSerialClient()
    fake.registers[_STATUS_REG] = _word(_BITS["on_normal_bit"], _BITS["normal_available_bit"])
    snap = await _reader(
        fake, transferring_bit=None, engine_start_bit=None
    ).read_inputs()
    assert snap.position == "utility"
    assert snap.engine_start_calling is False


async def test_assumed_mode_reported_in_snapshot():
    fake = FakeSerialClient()
    fake.registers[_STATUS_REG] = _word(_BITS["on_normal_bit"])
    snap = await _reader(fake, assumed_mode="manual").read_inputs()
    assert snap.ats_mode == "manual"


async def test_read_error_raises_oserror():
    fake = FakeSerialClient()
    fake.raise_on_read = True
    reader = _reader(fake)
    with pytest.raises(OSError, match="read_holding_registers"):
        await reader.read_inputs()
    # A read failure marks the client disconnected so the next call reconnects.
    assert reader._connected is False  # noqa: SLF001


def test_extract_bit_little_endian():
    assert _extract_bit([0b0000_0000_0000_0001], 0) is True
    assert _extract_bit([0b0000_0000_0000_0010], 0) is False
    assert _extract_bit([0, 0b100], 18) is True  # reg 1, bit 2
    assert _extract_bit([0xFFFF], 99) is False  # out of range → safe False


# ─── hybrid composition: reads from serial, outputs via the ADAM ──────────


def _hybrid(*, require_hw_watchdog: bool):
    """Build a hybrid with a utility-reading serial side and a fake ADAM."""
    serial = FakeSerialClient()
    serial.registers[_STATUS_REG] = _word(_BITS["on_normal_bit"],
                                          _BITS["normal_available_bit"])
    reader = _reader(serial)

    outputs = IOAdamDriver(host="127.0.0.1", require_hw_watchdog=require_hw_watchdog)
    adam_fake = FakeAdamClient()
    outputs._client = adam_fake  # noqa: SLF001
    outputs._connected = True  # noqa: SLF001
    return IOHybridDriver(reader=reader, outputs=outputs), adam_fake


async def test_hybrid_reads_inputs_from_serial():
    hybrid, _ = _hybrid(require_hw_watchdog=False)
    snap = await hybrid.read_inputs()
    assert snap.position == "utility"
    assert snap.normal_available is True


async def test_hybrid_drives_outputs_via_adam():
    hybrid, adam_fake = _hybrid(require_hw_watchdog=False)
    await hybrid.drive_outputs(inhibit=True)
    assert (DO_COIL_BASE + DO_INHIBIT, True) in adam_fake.writes


async def test_hybrid_preserves_f1_gate_when_watchdog_unverified():
    # require_hw_watchdog=True + never verified → asserting an output must be
    # refused, exactly as a plain ADAM driver would. The serial read path does
    # not weaken the control-side safety gate.
    hybrid, adam_fake = _hybrid(require_hw_watchdog=True)
    assert hybrid.hw_watchdog_ok() is False
    with pytest.raises(HwWatchdogNotArmedError):
        await hybrid.drive_outputs(inhibit=True)
    assert adam_fake.writes == []


async def test_hybrid_release_allowed_even_when_watchdog_unverified():
    # Releases are always the safe direction and must pass through the gate.
    hybrid, adam_fake = _hybrid(require_hw_watchdog=True)
    await hybrid.release_all_outputs()
    # DO 0-3 driven OFF.
    assert (DO_COIL_BASE + DO_INHIBIT, False) in adam_fake.writes


async def test_hybrid_hw_watchdog_status_delegates_to_adam():
    hybrid, _ = _hybrid(require_hw_watchdog=False)
    ok, detail = hybrid.hw_watchdog_status()
    assert ok is True
    assert "waived" in detail


async def test_hybrid_connect_returns_true_when_both_connect():
    hybrid, _ = _hybrid(require_hw_watchdog=False)
    assert await hybrid.connect() is True


# ─── CALIBRATION fault: impossible Group 5 status combination (ICD §5.1.1) ───


async def test_serial_both_position_bits_set_flags_calibration():
    """Both on_normal and on_emergency status bits set at once is an impossible
    combination — the serial reader must raise CALIBRATION (ICD §5.1.1) rather
    than publish only a bare position=unknown a consumer can't act on.
    """
    from atspi.io_driver import FAULT_CALIBRATION

    fake = FakeSerialClient()
    fake.registers[_STATUS_REG] = _word(_BITS["on_normal_bit"], _BITS["on_emergency_bit"])
    snap = await _reader(fake).read_inputs()
    assert snap.position == "unknown"
    assert snap.fault_bits & FAULT_CALIBRATION


async def test_serial_single_position_bit_is_not_calibration():
    """A legitimate single-source position carries no CALIBRATION bit."""
    from atspi.io_driver import FAULT_CALIBRATION

    fake = FakeSerialClient()
    fake.registers[_STATUS_REG] = _word(_BITS["on_normal_bit"], _BITS["normal_available_bit"])
    snap = await _reader(fake).read_inputs()
    assert snap.position == "utility"
    assert not (snap.fault_bits & FAULT_CALIBRATION)
