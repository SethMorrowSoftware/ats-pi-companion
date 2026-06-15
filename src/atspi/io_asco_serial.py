"""ASCO Group 5 controller reader — RS-485 Modbus RTU input backend.

Reads ATS state (switch position + source availability + engine-start)
directly from the ASCO Series 300 / Group 5 controller over its serial
port, via a USB-to-RS485 adapter on the Pi. This is the *monitoring*
half of the ``hybrid`` driver: it replaces the 18RX REX module and the
14AA/14BA auxiliary contacts (which a contact-only install needs for
DI 1-4) with a single serial link. The *control* half stays on the
ADAM-6060 (see ``io_hybrid`` and ``io_adam``), so the F1 hardware
fail-safe is unchanged.

  read_inputs() →  position, normal_available, emergency_available,
                   engine_start_calling   (all from the Group 5 map)

ASCO Group 5 Modbus map (ASCO doc 381339-221, "Connectivity to the
Power Manager Xp & 7000 Series Group 5 Controller via Modbus"):

  - RTU mode only, 8 data bits, no parity, 1 stop bit (8N1).
  - Status is exposed in *holding registers* (FC03). The slave address
    (1-247) and baud rate are set on the controller's front panel
    (General → Communication / RS485 port).

This reader treats one or more consecutive holding registers starting at
``status_register`` as a flat little-endian bit array (register 0 →
bits 0-15, register 1 → bits 16-31, …) and pulls each signal from a
configured bit index. That handles both a single packed status word and
a map that spreads the bits across several registers.

BENCH-VERIFY before deploying — every address/bit below is site-specific:

  1. The exact ``status_register`` and per-signal bit indices live in
     381339-221 and can shift by firmware revision (same discipline as
     the ADAM ``di_read`` / ``hw_watchdog`` addresses). They are left
     UNSET by default; the driver refuses to build until they are filled
     in (``AscoSerialConfig.is_configured``).
  2. Confirm the controller's RS485 address and baud match
     ``unit_id`` / ``baudrate`` (front panel, General → Communication).
  3. With the switch on Normal then on Emergency, read the register and
     confirm the position/availability bits track reality before trusting
     the published state — ``modpoll -m rtu -b <baud> -d 8 -p none -s 1
     -t 4 -r <reg+1> -c <count> <port>`` reads holding registers.

If your controller encodes position as an enum *value* rather than
discrete bits (some maps do), that's the documented extension point —
this v1 models discrete status bits.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from pymodbus.client import AsyncModbusSerialClient
from pymodbus.exceptions import ModbusException

from .io_adam import VALID_ASSUMED_MODES
from .io_driver import FAULT_CALIBRATION, InputSnapshot

log = logging.getLogger("atspi.io_asco_serial")

# Per-operation Modbus timeout default. The Group 5 responds to an RTU
# poll well within this; the sampling loop runs at 10 Hz so we keep it
# short enough not to stall a cycle (see io_adam.ADAM_TIMEOUT_S rationale).
DEFAULT_TIMEOUT_S = 1.0
SERIAL_RETRIES = 1


@dataclass(frozen=True)
class AscoSerialConfig:
    """Serial link parameters + the Group 5 holding-register / bit map.

    The bit indices are *flat* across the read block: bit ``b`` lives in
    register ``status_register + b // 16``, bit ``b % 16`` of that word.
    Required signals (position + both availabilities) must be configured;
    ``engine_start_bit`` and ``transferring_bit`` are optional.

    Every address/bit is BENCH-VERIFY from ASCO doc 381339-221 and is left
    UNSET (``None``) by default — ``is_configured`` is false until they are
    supplied, and the driver refuses to start rather than publish a guessed
    position for a live switch.
    """

    # ── RS-485 / serial link (8N1 per 381339-221) ──
    port: str = "/dev/ttyUSB0"
    baudrate: int = 19200
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1
    unit_id: int = 1  # controller RS485 address (1-247)
    timeout_s: float = DEFAULT_TIMEOUT_S

    # ATS mode reported in the snapshot. The serial map *can* expose the
    # controller's real Auto/Manual/Test mode, but mapping that is a
    # bench-verify follow-up; until then this is operator-asserted exactly
    # like io_adam.assumed_mode, and it gates command writes (ICD §6).
    assumed_mode: str = "auto"

    # ── Group 5 holding-register status map (FC03) ──
    status_register: int | None = None  # PDU offset (0-based) of the first word
    status_register_count: int = 1  # consecutive words read as one bit array
    on_normal_bit: int | None = None
    on_emergency_bit: int | None = None
    normal_available_bit: int | None = None
    emergency_available_bit: int | None = None
    # Optional: a dedicated "transfer in progress" bit (takes precedence over
    # the on_normal/on_emergency derivation when set) and an engine-start bit.
    transferring_bit: int | None = None
    engine_start_bit: int | None = None

    # The bits that MUST be configured for the reader to publish real state.
    _REQUIRED_BITS = (
        "on_normal_bit",
        "on_emergency_bit",
        "normal_available_bit",
        "emergency_available_bit",
    )

    def is_configured(self) -> bool:
        """status_register + every required bit index supplied → can publish."""
        if self.status_register is None:
            return False
        return all(getattr(self, name) is not None for name in self._REQUIRED_BITS)

    def _all_bit_indices(self) -> list[int]:
        return [
            b
            for b in (
                self.on_normal_bit,
                self.on_emergency_bit,
                self.normal_available_bit,
                self.emergency_available_bit,
                self.transferring_bit,
                self.engine_start_bit,
            )
            if b is not None
        ]

    def validate(self) -> None:
        """Raise ValueError on a structurally unusable config.

        Caught at driver-build time so a hybrid deployment fails fast with a
        clear message instead of silently publishing ``unknown`` forever.
        """
        if not self.is_configured():
            missing = [n for n in self._REQUIRED_BITS if getattr(self, n) is None]
            if self.status_register is None:
                missing = ["status_register", *missing]
            raise ValueError(
                "io.asco_serial is incomplete: set "
                + ", ".join(missing)
                + " from ASCO doc 381339-221 (and bench-verify). The hybrid "
                "driver will not publish a guessed switch position."
            )
        if self.assumed_mode not in VALID_ASSUMED_MODES:
            raise ValueError(
                f"io.asco_serial.assumed_mode={self.assumed_mode!r} invalid; "
                f"expected one of {sorted(VALID_ASSUMED_MODES)}"
            )
        if self.status_register_count < 1:
            raise ValueError("io.asco_serial.status_register_count must be >= 1")
        capacity = self.status_register_count * 16
        for b in self._all_bit_indices():
            if not (0 <= b < capacity):
                raise ValueError(
                    f"io.asco_serial bit index {b} is outside the "
                    f"{self.status_register_count}-register read block "
                    f"(0..{capacity - 1}); raise status_register_count or fix the bit"
                )


def _extract_bit(registers: list[int], flat_bit: int) -> bool:
    """Bit ``flat_bit`` of the registers read as one little-endian bit array."""
    reg_index, bit_in_reg = divmod(flat_bit, 16)
    if reg_index >= len(registers):
        # validate() guarantees this can't happen given status_register_count,
        # but guard rather than IndexError if a short read ever slips through.
        return False
    return bool(registers[reg_index] & (1 << bit_in_reg))


class AscoSerialReader:
    """Reads the Group 5 controller's state over Modbus RTU (RS-485).

    Implements only the *input* side of the I/O contract (connect / close /
    read_inputs); ``io_hybrid`` pairs it with an ``IOAdamDriver`` for the
    output side. Async, with lazy reconnect mirroring io_adam.
    """

    def __init__(self, cfg: AscoSerialConfig):
        cfg.validate()
        self._cfg = cfg
        self._client: AsyncModbusSerialClient | None = None
        self._connected = False

    async def connect(self) -> bool:
        if self._client is None:
            # AsyncModbusSerialClient defaults to the RTU framer, which is what
            # 381339-221 requires (RTU mode only).
            self._client = AsyncModbusSerialClient(
                port=self._cfg.port,
                baudrate=self._cfg.baudrate,
                bytesize=self._cfg.bytesize,
                parity=self._cfg.parity,
                stopbits=self._cfg.stopbits,
                timeout=self._cfg.timeout_s,
                retries=SERIAL_RETRIES,
            )
        try:
            ok = await self._client.connect()
        except Exception as e:  # noqa: BLE001
            log.warning("ASCO serial open of %s failed: %s", self._cfg.port, e)
            ok = False
        self._connected = bool(ok)
        if self._connected:
            log.info(
                "ASCO Group 5 reader open on %s @ %d %d%s%d (RTU, addr %d)",
                self._cfg.port, self._cfg.baudrate, self._cfg.bytesize,
                self._cfg.parity, self._cfg.stopbits, self._cfg.unit_id,
            )
        return self._connected

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        self._connected = False

    async def read_inputs(self) -> InputSnapshot:
        regs = await self._read_status_registers()
        cfg = self._cfg

        on_normal = _extract_bit(regs, cfg.on_normal_bit)
        on_emergency = _extract_bit(regs, cfg.on_emergency_bit)
        transferring = (
            cfg.transferring_bit is not None
            and _extract_bit(regs, cfg.transferring_bit)
        )

        if transferring:
            position = "transferring"
        elif on_normal and not on_emergency:
            position = "utility"
        elif on_emergency and not on_normal:
            position = "generator"
        else:
            # Both set (fault) or neither set (mid-stroke with no transfer bit).
            position = "unknown"

        engine_start = (
            cfg.engine_start_bit is not None
            and _extract_bit(regs, cfg.engine_start_bit)
        )

        # ICD §5.1.1 CALIBRATION: both on_normal and on_emergency set at once is
        # a physically impossible contact combination (a welded/miswired status
        # bit), distinct from the legitimate both-off mid-stroke — surface it so
        # GenWatch sees a fault rather than only a bare position=unknown.
        fault_bits = FAULT_CALIBRATION if (on_normal and on_emergency) else 0

        return InputSnapshot(
            position=position,
            normal_available=_extract_bit(regs, cfg.normal_available_bit),
            emergency_available=_extract_bit(regs, cfg.emergency_available_bit),
            engine_start_calling=engine_start,
            ats_mode=cfg.assumed_mode,
            fault_bits=fault_bits,
        )

    # ─── Internal: Modbus RTU access with implicit reconnect ─────────────

    async def _ensure_connected(self) -> None:
        if self._connected and self._client is not None and self._client.connected:
            return
        if not self._connected and self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        await self.connect()
        if not self._connected:
            raise ConnectionError(f"ASCO Group 5 unreachable on {self._cfg.port}")

    async def _read_status_registers(self) -> list[int]:
        await self._ensure_connected()
        addr = self._cfg.status_register
        count = self._cfg.status_register_count
        try:
            rr = await self._client.read_holding_registers(
                address=addr, count=count, slave=self._cfg.unit_id
            )
        except (TimeoutError, ModbusException, ConnectionError) as e:
            self._connected = False
            raise OSError(
                f"ASCO read_holding_registers({addr}, {count}) failed: {e}"
            ) from e
        if rr.isError():
            raise OSError(f"ASCO read_holding_registers({addr}, {count}) error: {rr}")
        return list(rr.registers[:count])


__all__ = ["AscoSerialConfig", "AscoSerialReader"]
