"""In-memory register store implementing the ICD §5 register layout.

Single source of truth for ATS-Pi state. The sampling loop writes via
``apply_input_snapshot`` / ``apply_output_state``; the Modbus server
reads via ``read_register``; write-side Modbus calls land here via
``write_register``.

All multi-word reads MUST publish a coherent snapshot — never a
half-updated state. This is achieved by computing the new values off
to the side and then assigning the full bag at once.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from . import ICD_VERSION, __version__
from .io_driver import InputSnapshot, OutputState

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


@dataclass
class _StateSnapshot:
    """Snapshot of all internal state. Atomically swapped under lock
    so Modbus reads always see a coherent set of values.
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


# Fault-summary bit masks (ICD §5.1.1)
FAULT_INPUT = 0x0001
FAULT_OUTPUT = 0x0002
FAULT_MODE_UNKNOWN = 0x0004
FAULT_CALIBRATION = 0x0008


class RegisterStore:
    """ICD-compliant state model. Thread-safe (the Modbus server may
    run reads on the asyncio event loop while the sampling loop writes
    from a separate task — same loop in practice but we keep the lock
    for clarity).
    """

    def __init__(self, unit_id: int = 1):
        self._unit_id = unit_id
        self._fw_version = (0, 1, 0)
        self._icd_version = ICD_VERSION
        self._snap = _StateSnapshot()
        self._lock = threading.Lock()
        self._boot_ts = time.time()

    # ─── Sampling-loop writers ────────────────────────────────────────

    def apply_input_snapshot(self, inputs: InputSnapshot) -> None:
        with self._lock:
            prev = self._snap
            new = _StateSnapshot(
                position=inputs.position,
                normal_available=inputs.normal_available,
                emergency_available=inputs.emergency_available,
                engine_start_calling=inputs.engine_start_calling,
                ats_mode=inputs.ats_mode,
                fault_bits=inputs.fault_bits,
                last_transfer_to_gen_ts=prev.last_transfer_to_gen_ts,
                last_retransfer_to_util_ts=prev.last_retransfer_to_util_ts,
                transfer_count_lifetime=prev.transfer_count_lifetime,
                transfer_count_24h=prev.transfer_count_24h,
                cmd_test_active=prev.cmd_test_active,
                cmd_inhibit_active=prev.cmd_inhibit_active,
                cmd_force_transfer_active=prev.cmd_force_transfer_active,
                cmd_bypass_delay_active=prev.cmd_bypass_delay_active,
            )

            # Track transitions for counters and timestamps
            now = int(time.time())
            if prev.position != "generator" and new.position == "generator":
                new.last_transfer_to_gen_ts = now
                new.transfer_count_lifetime = prev.transfer_count_lifetime + 1
                new.transfer_count_24h = prev.transfer_count_24h + 1
            elif prev.position == "generator" and new.position == "utility":
                new.last_retransfer_to_util_ts = now

            self._snap = new

    def apply_output_state(self, outputs: OutputState) -> None:
        with self._lock:
            self._snap.cmd_test_active = outputs.test_active
            self._snap.cmd_inhibit_active = outputs.inhibit_active
            self._snap.cmd_force_transfer_active = outputs.force_transfer_active
            self._snap.cmd_bypass_delay_active = outputs.bypass_delay_active

    def set_input_fault(self, on: bool) -> None:
        with self._lock:
            if on:
                self._snap.fault_bits |= FAULT_INPUT
            else:
                self._snap.fault_bits &= ~FAULT_INPUT

    def release_maintained_commands(self) -> None:
        """Called by the safety watchdog (ICD §8.3) on comms timeout.

        Clears the cmd_inhibit and cmd_force_transfer read-back state.
        The actual relay release on the ADAM must be issued separately
        through the I/O driver — this method only clears the store.
        """
        with self._lock:
            self._snap.cmd_inhibit_active = False
            self._snap.cmd_force_transfer_active = False

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

    def write_register(self, addr: int, value: int) -> bool:
        """Handle a write from a Modbus client.

        Phase 1 stub — actually routing this to the I/O driver to drive
        physical outputs happens in implementation Phase C (see SPEC §8).
        Returns True if the address+value was a recognized command.

        TODO: this currently only mirrors writes into the read-back
        store. Wire to the IODriver so the ASCO inputs are actually
        driven.
        """
        if addr == ADDR_CMD_TEST and value == 0x0001:
            with self._lock:
                self._snap.cmd_test_active = True
            return True
        if addr == ADDR_CMD_INHIBIT and value in (0x0000, 0x0001):
            with self._lock:
                self._snap.cmd_inhibit_active = value == 0x0001
            return True
        if addr == ADDR_CMD_FORCE_TRANSFER and value in (0x0000, 0x0001):
            with self._lock:
                self._snap.cmd_force_transfer_active = value == 0x0001
            return True
        if addr == ADDR_CMD_BYPASS_DELAY and value == 0x0001:
            with self._lock:
                self._snap.cmd_bypass_delay_active = True
            return True
        return False
