"""Advantech ADAM-6060 driver — production I/O backend.

Implements the abstract :class:`IODriver` against an ADAM-6060 (6 DI +
6 relay DO) over Modbus TCP. Channel-to-signal mapping is the one
documented in ``docs/HARDWARE.md §3``:

  DI 0 → Load Disconnect contact (pulse → position=transferring)
  DI 1 → On Normal aux (14AA)
  DI 2 → On Emergency aux (14BA)
  DI 3 → Normal source available (18RX RL6)
  DI 4 → Emergency source available (18RX RL5)
  DI 5 → Engine-start sense

  DO 0 → Momentary Test pulse
  DO 1 → Maintained Force Transfer
  DO 2 → Maintained Inhibit
  DO 3 → Bypass Transfer Time Delay pulse

ADAM-6060 Modbus map (per Advantech ADAM-6000 User Manual rev A4,
verify against the firmware on the actual unit before commissioning):

  Read coils  (FC01) 00001-00006 → DI 0..5
  Read coils  (FC01) 00017-00022 → DO 0..5 (read-back of relay state)
  Write coil  (FC05) 00017-00022 → set DO 0..5

The implementation uses coil access for both directions because it
maps cleanly to single bits and works on every ADAM-6060 firmware
revision in the field. Holding-register packed reads are an alternative
but their bit layout shifted between firmware revisions.

BENCH-VERIFY before deploying:

  1. Confirm DI coil base (some revisions start at 00001, others 10001
     — pymodbus's ``read_discrete_inputs`` vs ``read_coils``).
  2. Confirm DO coil base for read-back.
  3. Confirm bit ordering matches the labelling on the unit's terminals.
  4. Drive each DO individually and verify the matching ATS terminal
     responds before connecting all six at once.
"""
from __future__ import annotations

import asyncio
import logging
import time

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from .io_driver import InputSnapshot, OutputState

log = logging.getLogger("atspi.io_adam")


# ADAM-6060 coil addresses (PDU offsets, 0-based).
DI_COIL_BASE = 0x0000  # DI 0..5
DO_COIL_BASE = 0x0010  # DO 0..5 (read-back and write)

# DI channel assignments per HARDWARE.md §3.
DI_LOAD_DISCONNECT = 0
DI_ON_NORMAL = 1
DI_ON_EMERGENCY = 2
DI_NORMAL_AVAIL = 3
DI_EMERGENCY_AVAIL = 4
DI_ENGINE_START = 5

# DO channel assignments per HARDWARE.md §3.
DO_TEST = 0
DO_FORCE_TRANSFER = 1
DO_INHIBIT = 2
DO_BYPASS_DELAY = 3

# ICD §6.1 pulse range.
PULSE_MIN_MS = 500
PULSE_MAX_MS = 1500

# The Load Disconnect contact pulses momentarily during a transfer;
# we hold "transferring" position for a short window after we see it
# so a 10 Hz sampling loop reliably catches it.
TRANSFERRING_HOLD_S = 2.0

# After driving a relay, allow up to this long for the actual coil read-back
# to catch up before flagging a mismatch. The ADAM's internal scan cycle is
# nominally ≤100 ms; 500 ms gives generous headroom for one full Modbus
# read/write round-trip plus the relay's own actuation delay.
OUTPUT_SETTLING_S = 0.5


def _bit_pulse(timestamp_mono: float | None, hold_s: float, now_mono: float) -> bool:
    if timestamp_mono is None:
        return False
    return (now_mono - timestamp_mono) < hold_s


class IOAdamDriver:
    """ADAM-6060 driver. Async, retries lazily, fault bits surface in
    the register store via :meth:`set_output_fault` from the sampling
    loop when a write fails.
    """

    def __init__(self, host: str, port: int = 502, unit_id: int = 1):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self._client: AsyncModbusTcpClient | None = None
        self._connected = False

        # Pulse-release scheduling (the ADAM has no notion of pulse;
        # we drive the relay high then schedule a low write).
        self._test_release_task: asyncio.Task | None = None
        self._bypass_release_task: asyncio.Task | None = None

        # Last time we saw DI 0 (Load Disconnect) asserted. Used to
        # report "transferring" position for a brief hold window since
        # the contact is a momentary pulse, not a maintained state.
        self._load_disconnect_seen_mono: float | None = None

        # Stuck-relay detection. Tracks the last value commanded to each
        # DO and the monotonic timestamp of that command. Read by
        # check_output_consistency() to compare against actual read-back.
        # Pulse release tasks update this with (False, now) when they fire.
        self._commanded_do: dict[int, tuple[bool, float]] = {}

    async def connect(self) -> bool:
        if self._client is None:
            self._client = AsyncModbusTcpClient(host=self.host, port=self.port)
        try:
            ok = await self._client.connect()
        except Exception as e:  # noqa: BLE001
            log.warning("ADAM connect to %s:%d failed: %s", self.host, self.port, e)
            ok = False
        self._connected = bool(ok)
        if self._connected:
            log.info("ADAM-6060 connected at %s:%d", self.host, self.port)
        return self._connected

    async def close(self) -> None:
        for t in (self._test_release_task, self._bypass_release_task):
            if t is not None and not t.done():
                t.cancel()
        if self._client is not None:
            self._client.close()
            self._client = None
        self._connected = False

    async def read_inputs(self) -> InputSnapshot:
        bits = await self._read_coils(DI_COIL_BASE, 6)
        now_mono = time.monotonic()

        if bits[DI_LOAD_DISCONNECT]:
            self._load_disconnect_seen_mono = now_mono

        on_normal = bits[DI_ON_NORMAL]
        on_emerg = bits[DI_ON_EMERGENCY]
        transferring = _bit_pulse(self._load_disconnect_seen_mono, TRANSFERRING_HOLD_S, now_mono)

        if transferring:
            position = "transferring"
        elif on_normal and not on_emerg:
            position = "utility"
        elif on_emerg and not on_normal:
            position = "generator"
        else:
            # Both off (mid-stroke) or both on (impossible / fault)
            position = "unknown"

        return InputSnapshot(
            position=position,
            normal_available=bits[DI_NORMAL_AVAIL],
            emergency_available=bits[DI_EMERGENCY_AVAIL],
            engine_start_calling=bits[DI_ENGINE_START],
            ats_mode="auto",  # ADAM-6060 has no Auto/Manual sense contact
            fault_bits=0,
        )

    async def read_output_state(self) -> OutputState:
        bits = await self._read_coils(DO_COIL_BASE, 6)
        return OutputState(
            test_active=bits[DO_TEST],
            inhibit_active=bits[DO_INHIBIT],
            force_transfer_active=bits[DO_FORCE_TRANSFER],
            bypass_delay_active=bits[DO_BYPASS_DELAY],
        )

    async def drive_outputs(
        self,
        *,
        test_pulse_ms: int | None = None,
        inhibit: bool | None = None,
        force_transfer: bool | None = None,
        bypass_delay_pulse_ms: int | None = None,
    ) -> None:
        if test_pulse_ms is not None:
            await self._pulse(DO_TEST, "test", test_pulse_ms)
        if inhibit is not None:
            await self._write_coil(DO_COIL_BASE + DO_INHIBIT, bool(inhibit))
            self._record_commanded(DO_INHIBIT, bool(inhibit))
            log.info("ADAM: inhibit %s", "ASSERT" if inhibit else "RELEASE")
        if force_transfer is not None:
            await self._write_coil(DO_COIL_BASE + DO_FORCE_TRANSFER, bool(force_transfer))
            self._record_commanded(DO_FORCE_TRANSFER, bool(force_transfer))
            log.info("ADAM: force_transfer %s", "ASSERT" if force_transfer else "RELEASE")
        if bypass_delay_pulse_ms is not None:
            await self._pulse(DO_BYPASS_DELAY, "bypass_delay", bypass_delay_pulse_ms)

    def _record_commanded(self, do_index: int, value: bool) -> None:
        """Note what we just drove onto ``do_index`` for stuck-relay detection."""
        self._commanded_do[do_index] = (value, time.monotonic())

    def check_output_consistency(self, actual: OutputState) -> bool:
        """Compare ``actual`` (just read from the ADAM) against the last
        commanded state of each DO. Within OUTPUT_SETTLING_S of a write
        any mismatch is tolerated (relay actuation + ADAM scan latency).
        Past that window, a mismatch indicates a stuck relay or
        miswired DO.
        """
        now = time.monotonic()
        actual_for_do = {
            DO_TEST: actual.test_active,
            DO_FORCE_TRANSFER: actual.force_transfer_active,
            DO_INHIBIT: actual.inhibit_active,
            DO_BYPASS_DELAY: actual.bypass_delay_active,
        }
        for do_index, (cmd_value, cmd_ts) in self._commanded_do.items():
            if now - cmd_ts < OUTPUT_SETTLING_S:
                continue
            actual_value = actual_for_do.get(do_index)
            if actual_value is None:
                continue
            if actual_value != cmd_value:
                log.warning(
                    "ADAM DO%d read-back mismatch: commanded=%s actual=%s "
                    "(%.1fs since command) — possible stuck relay",
                    do_index, cmd_value, actual_value, now - cmd_ts,
                )
                return False
        return True

    # ─── Internal: pulse handling ─────────────────────────────────────

    async def _pulse(self, do_index: int, name: str, duration_ms: int) -> None:
        # ICD §6: writes during an active pulse are IGNORED — the original
        # pulse runs to its scheduled completion without being re-triggered
        # or extended.
        slot = "_test_release_task" if do_index == DO_TEST else "_bypass_release_task"
        prior = getattr(self, slot)
        if prior is not None and not prior.done():
            log.debug("ADAM: %s already pulsing; ignoring re-trigger", name)
            return

        ms = max(PULSE_MIN_MS, min(PULSE_MAX_MS, int(duration_ms)))
        coil = DO_COIL_BASE + do_index
        await self._write_coil(coil, True)
        self._record_commanded(do_index, True)
        log.info("ADAM: pulsing %s for %d ms", name, ms)

        setattr(self, slot, asyncio.create_task(self._release(coil, do_index, name, ms)))

    async def _release(self, coil: int, do_index: int, name: str, after_ms: int) -> None:
        try:
            await asyncio.sleep(after_ms / 1000.0)
            await self._write_coil(coil, False)
            self._record_commanded(do_index, False)
            log.info("ADAM: pulsed %s released", name)
        except asyncio.CancelledError:
            pass

    # ─── Internal: Modbus access with implicit reconnect ─────────────

    async def _ensure_connected(self) -> None:
        if self._connected and self._client is not None and self._client.connected:
            return
        # If a previous read/write set self._connected=False, the pymodbus
        # client may be in a half-open state where .connect() returns True
        # but subsequent operations still time out. Close and recreate so
        # we start from a clean socket. (We don't recreate when _connected
        # is still True; in that case the client just needs reconnect().)
        if not self._connected and self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        await self.connect()
        if not self._connected:
            raise ConnectionError(f"ADAM-6060 unreachable at {self.host}:{self.port}")

    async def _read_coils(self, address: int, count: int) -> list[bool]:
        await self._ensure_connected()
        try:
            rr = await self._client.read_coils(address=address, count=count, slave=self.unit_id)
        except (TimeoutError, ModbusException, ConnectionError) as e:
            self._connected = False
            raise OSError(f"ADAM read_coils({address}, {count}) failed: {e}") from e
        if rr.isError():
            raise OSError(f"ADAM read_coils({address}, {count}) error: {rr}")
        # pymodbus returns more bits than requested (rounded to byte); trim.
        return list(rr.bits[:count])

    async def _write_coil(self, address: int, value: bool) -> None:
        await self._ensure_connected()
        try:
            wr = await self._client.write_coil(address=address, value=value, slave=self.unit_id)
        except (TimeoutError, ModbusException, ConnectionError) as e:
            self._connected = False
            raise OSError(f"ADAM write_coil({address}, {value}) failed: {e}") from e
        if wr.isError():
            raise OSError(f"ADAM write_coil({address}, {value}) error: {wr}")


__all__ = ["IOAdamDriver"]
