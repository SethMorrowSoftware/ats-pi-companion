"""In-memory register store implementing the ICD §5 register layout.

Single source of truth for ATS-Pi state. The sampling loop writes via
``apply_input_snapshot`` / ``apply_output_state``; the Modbus server
reads via ``read_register``; write-side Modbus calls land here via
``write_register``, which returns a :class:`CommandIntent` so the
server layer can dispatch the work to the I/O driver.

All multi-word reads MUST publish a coherent snapshot — never a
half-updated state. This is achieved by computing the new values off
to the side under the lock and assigning the full snapshot at once.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, replace

from . import ICD_VERSION, __version__
from .io_driver import InputSnapshot, OutputState
from .persistence import PersistedState, StateFile

log = logging.getLogger("atspi.state")


# ICD §5 address constants (PDU offsets, hex). Keep in sync with the ICD.
ADDR_POSITION = 0x0000
ADDR_NORMAL_AVAIL = 0x0001
ADDR_EMERGENCY_AVAIL = 0x0002
ADDR_ENGINE_START_CALLING = 0x0003
ADDR_ATS_MODE = 0x0004
ADDR_FAULT_SUMMARY = 0x0005

ADDR_LAST_TRANSFER_TS = 0x0010
ADDR_LAST_RETRANSFER_TS = 0x0012
ADDR_UPTIME_S = 0x0014
ADDR_WALLCLOCK = 0x0016

ADDR_TRANSFER_COUNT_LIFETIME = 0x0020
ADDR_TRANSFER_COUNT_24H = 0x0022

ADDR_ICD_MAJOR = 0x0030
ADDR_ICD_MINOR = 0x0031
ADDR_FW_MAJOR = 0x0032
ADDR_FW_MINOR = 0x0033
ADDR_FW_PATCH = 0x0034
ADDR_UNIT_ID = 0x0035

ADDR_CMD_TEST_RB = 0x0040
ADDR_CMD_INHIBIT_RB = 0x0041
ADDR_CMD_FORCE_TRANSFER_RB = 0x0042
ADDR_CMD_BYPASS_DELAY_RB = 0x0043

ADDR_CMD_TEST = 0x0100
ADDR_CMD_INHIBIT = 0x0101
ADDR_CMD_FORCE_TRANSFER = 0x0102
ADDR_CMD_BYPASS_DELAY = 0x0103

# Enum mappings (ICD §5.1)
_POSITION_TO_VALUE = {
    "utility": 0, "generator": 1, "transferring": 2, "unknown": 3,
}
_MODE_TO_VALUE = {"auto": 0, "manual": 1, "test": 2, "unknown": 3}

# Default pulse duration when a Modbus client sets cmd_test or cmd_bypass_delay
# without specifying one. Sits in the middle of the ICD §6.1 range.
DEFAULT_PULSE_MS = 750
ROLLING_WINDOW_S = 24 * 3600


# Fault-summary bit masks (ICD §5.1.1)
FAULT_INPUT = 0x0001
FAULT_OUTPUT = 0x0002
FAULT_MODE_UNKNOWN = 0x0004
FAULT_CALIBRATION = 0x0008

# Bits the orchestrator owns (set/cleared by set_input_fault / set_output_fault).
# Other bits in fault_summary are sourced from the driver via InputSnapshot.fault_bits.
_LOCAL_FAULT_MASK = FAULT_INPUT | FAULT_OUTPUT


@dataclass(frozen=True)
class _StateSnapshot:
    """Immutable snapshot of all internal state. Atomically swapped
    under lock so Modbus reads always see a coherent set of values.
    """
    position: str = "unknown"
    normal_available: bool = False
    emergency_available: bool = False
    engine_start_calling: bool = False
    ats_mode: str = "unknown"
    fault_bits: int = 0

    last_transfer_to_gen_ts: int = 0
    last_retransfer_to_util_ts: int = 0
    transfer_count_lifetime: int = 0
    transfer_count_24h: int = 0

    cmd_test_active: bool = False
    cmd_inhibit_active: bool = False
    cmd_force_transfer_active: bool = False
    cmd_bypass_delay_active: bool = False


@dataclass(frozen=True)
class CommandIntent:
    """Recognized Modbus write translated into a driver-level command.

    The server layer translates this into the matching
    ``IODriver.drive_outputs`` call. Pulse fields are populated for
    pulsed commands (test, bypass_delay); maintained fields for inhibit
    and force_transfer.
    """

    test_pulse_ms: int | None = None
    inhibit: bool | None = None
    force_transfer: bool | None = None
    bypass_delay_pulse_ms: int | None = None


class RegisterStore:
    """ICD-compliant state model. Thread-safe (the Modbus server runs
    reads on the asyncio event loop while the sampling loop writes
    from a separate task — same loop in practice but we keep the lock
    for clarity).
    """

    def __init__(
        self,
        unit_id: int = 1,
        state_file: StateFile | None = None,
    ):
        self._unit_id = unit_id
        self._fw_version = tuple(int(p) for p in __version__.split(".")[:3])
        if len(self._fw_version) < 3:
            self._fw_version = self._fw_version + (0,) * (3 - len(self._fw_version))
        self._icd_version = ICD_VERSION
        self._lock = threading.Lock()
        self._boot_ts = time.time()
        self._state_file = state_file
        self._transfer_timestamps: deque[float] = deque()

        persisted = state_file.load() if state_file else PersistedState()
        self._snap = _StateSnapshot(
            last_transfer_to_gen_ts=persisted.last_transfer_to_gen_ts,
            last_retransfer_to_util_ts=persisted.last_retransfer_to_util_ts,
            transfer_count_lifetime=persisted.transfer_count_lifetime,
        )

    # ─── Sampling-loop writers ────────────────────────────────────────

    def apply_input_snapshot(self, inputs: InputSnapshot) -> None:
        """Apply a fresh input read. Atomic snapshot swap; transitions
        update counters and timestamps.
        """
        now_wall = int(time.time())
        now_mono = time.monotonic()
        persist = False

        with self._lock:
            prev = self._snap
            new_last_to_gen = prev.last_transfer_to_gen_ts
            new_last_to_util = prev.last_retransfer_to_util_ts
            new_lifetime = prev.transfer_count_lifetime

            transferred_to_gen = (
                prev.position != "generator" and inputs.position == "generator"
            )
            retransferred_to_util = (
                prev.position == "generator" and inputs.position == "utility"
            )

            if transferred_to_gen:
                new_last_to_gen = now_wall
                new_lifetime = prev.transfer_count_lifetime + 1
                self._transfer_timestamps.append(now_mono)
                persist = True
            elif retransferred_to_util:
                new_last_to_util = now_wall
                persist = True

            self._evict_old_transfers(now_mono)

            # FAULT_INPUT and FAULT_OUTPUT are owned by the sampling-loop and
            # command-dispatch paths via set_input_fault / set_output_fault.
            # Other fault bits (MODE_UNKNOWN, CALIBRATION) come from the driver.
            # Without this merge, a successful read would clobber OUTPUT_FAULT
            # set by a recently-failed drive_outputs call.
            merged_fault_bits = (
                (prev.fault_bits & _LOCAL_FAULT_MASK)
                | (inputs.fault_bits & ~_LOCAL_FAULT_MASK)
            ) & 0xFFFF

            self._snap = _StateSnapshot(
                position=inputs.position,
                normal_available=inputs.normal_available,
                emergency_available=inputs.emergency_available,
                engine_start_calling=inputs.engine_start_calling,
                ats_mode=inputs.ats_mode,
                fault_bits=merged_fault_bits,
                last_transfer_to_gen_ts=new_last_to_gen,
                last_retransfer_to_util_ts=new_last_to_util,
                transfer_count_lifetime=new_lifetime,
                transfer_count_24h=len(self._transfer_timestamps),
                cmd_test_active=prev.cmd_test_active,
                cmd_inhibit_active=prev.cmd_inhibit_active,
                cmd_force_transfer_active=prev.cmd_force_transfer_active,
                cmd_bypass_delay_active=prev.cmd_bypass_delay_active,
            )

        if persist and self._state_file is not None:
            self._persist_async_safe()

    def apply_output_state(self, outputs: OutputState) -> None:
        """Apply a fresh output read. Snapshot-swap (no mutate-in-place)
        keeps the atomicity invariant from ICD §8.1.
        """
        with self._lock:
            self._snap = replace(
                self._snap,
                cmd_test_active=outputs.test_active,
                cmd_inhibit_active=outputs.inhibit_active,
                cmd_force_transfer_active=outputs.force_transfer_active,
                cmd_bypass_delay_active=outputs.bypass_delay_active,
            )

    def set_input_fault(self, on: bool) -> None:
        with self._lock:
            bits = self._snap.fault_bits
            bits = (bits | FAULT_INPUT) if on else (bits & ~FAULT_INPUT)
            self._snap = replace(self._snap, fault_bits=bits & 0xFFFF)

    def set_output_fault(self, on: bool) -> None:
        with self._lock:
            bits = self._snap.fault_bits
            bits = (bits | FAULT_OUTPUT) if on else (bits & ~FAULT_OUTPUT)
            self._snap = replace(self._snap, fault_bits=bits & 0xFFFF)

    def release_maintained_commands(self) -> None:
        """Called by the safety watchdog (ICD §8.3) on comms timeout.

        Clears the cmd_inhibit and cmd_force_transfer read-back state.
        The actual relay release on the ADAM must be issued separately
        through the I/O driver — this method only clears the store.
        """
        with self._lock:
            self._snap = replace(
                self._snap,
                cmd_inhibit_active=False,
                cmd_force_transfer_active=False,
            )

    # ─── Modbus server-side ───────────────────────────────────────────

    def read_register(self, addr: int) -> int:
        """Return the 16-bit value at the given PDU address."""
        with self._lock:
            s = self._snap

            # Core state
            if addr == ADDR_POSITION:
                return _POSITION_TO_VALUE.get(s.position, 3)
            if addr == ADDR_NORMAL_AVAIL:
                return int(s.normal_available)
            if addr == ADDR_EMERGENCY_AVAIL:
                return int(s.emergency_available)
            if addr == ADDR_ENGINE_START_CALLING:
                return int(s.engine_start_calling)
            if addr == ADDR_ATS_MODE:
                return _MODE_TO_VALUE.get(s.ats_mode, 3)
            if addr == ADDR_FAULT_SUMMARY:
                return s.fault_bits & 0xFFFF

            # u32 fields, high word at lower address
            if addr == ADDR_LAST_TRANSFER_TS:
                return (s.last_transfer_to_gen_ts >> 16) & 0xFFFF
            if addr == ADDR_LAST_TRANSFER_TS + 1:
                return s.last_transfer_to_gen_ts & 0xFFFF
            if addr == ADDR_LAST_RETRANSFER_TS:
                return (s.last_retransfer_to_util_ts >> 16) & 0xFFFF
            if addr == ADDR_LAST_RETRANSFER_TS + 1:
                return s.last_retransfer_to_util_ts & 0xFFFF
            if addr == ADDR_UPTIME_S:
                up = int(time.time() - self._boot_ts)
                return (up >> 16) & 0xFFFF
            if addr == ADDR_UPTIME_S + 1:
                up = int(time.time() - self._boot_ts)
                return up & 0xFFFF
            if addr == ADDR_WALLCLOCK:
                wc = int(time.time())
                return (wc >> 16) & 0xFFFF
            if addr == ADDR_WALLCLOCK + 1:
                return int(time.time()) & 0xFFFF
            if addr == ADDR_TRANSFER_COUNT_LIFETIME:
                return (s.transfer_count_lifetime >> 16) & 0xFFFF
            if addr == ADDR_TRANSFER_COUNT_LIFETIME + 1:
                return s.transfer_count_lifetime & 0xFFFF
            if addr == ADDR_TRANSFER_COUNT_24H:
                return (s.transfer_count_24h >> 16) & 0xFFFF
            if addr == ADDR_TRANSFER_COUNT_24H + 1:
                return s.transfer_count_24h & 0xFFFF

            # Identification
            if addr == ADDR_ICD_MAJOR:
                return self._icd_version[0]
            if addr == ADDR_ICD_MINOR:
                return self._icd_version[1]
            if addr == ADDR_FW_MAJOR:
                return self._fw_version[0]
            if addr == ADDR_FW_MINOR:
                return self._fw_version[1]
            if addr == ADDR_FW_PATCH:
                return self._fw_version[2]
            if addr == ADDR_UNIT_ID:
                return self._unit_id

            # Command read-back
            if addr == ADDR_CMD_TEST_RB:
                return int(s.cmd_test_active)
            if addr == ADDR_CMD_INHIBIT_RB:
                return int(s.cmd_inhibit_active)
            if addr == ADDR_CMD_FORCE_TRANSFER_RB:
                return int(s.cmd_force_transfer_active)
            if addr == ADDR_CMD_BYPASS_DELAY_RB:
                return int(s.cmd_bypass_delay_active)

            # RESERVED / unknown → 0 per ICD §5
            return 0

    def write_register(self, addr: int, value: int) -> CommandIntent | None:
        """Translate a Modbus write into a driver-level command intent.

        Returns the :class:`CommandIntent` the server layer should
        dispatch to the I/O driver, or ``None`` if the write is
        unrecognized. The store does NOT mutate read-back state itself —
        that happens once the next sampling cycle reads the driver's
        actual output state. This keeps the read-back register honest
        (it reflects what the relay actually is, not what we asked for).
        """
        if addr == ADDR_CMD_TEST and value == 0x0001:
            return CommandIntent(test_pulse_ms=DEFAULT_PULSE_MS)
        if addr == ADDR_CMD_INHIBIT and value in (0x0000, 0x0001):
            return CommandIntent(inhibit=value == 0x0001)
        if addr == ADDR_CMD_FORCE_TRANSFER and value in (0x0000, 0x0001):
            return CommandIntent(force_transfer=value == 0x0001)
        if addr == ADDR_CMD_BYPASS_DELAY and value == 0x0001:
            return CommandIntent(bypass_delay_pulse_ms=DEFAULT_PULSE_MS)
        return None

    # ─── Internal helpers ─────────────────────────────────────────────

    def _evict_old_transfers(self, now_mono: float) -> None:
        cutoff = now_mono - ROLLING_WINDOW_S
        while self._transfer_timestamps and self._transfer_timestamps[0] < cutoff:
            self._transfer_timestamps.popleft()

    def _persist_async_safe(self) -> None:
        """Snapshot persisted fields under the lock, then write outside.
        Catches I/O errors so a flaky disk never crashes the service.
        """
        with self._lock:
            persisted = PersistedState(
                transfer_count_lifetime=self._snap.transfer_count_lifetime,
                last_transfer_to_gen_ts=self._snap.last_transfer_to_gen_ts,
                last_retransfer_to_util_ts=self._snap.last_retransfer_to_util_ts,
            )
        try:
            self._state_file.save(persisted)
        except OSError as e:
            log.warning("failed to persist state: %s", e)
