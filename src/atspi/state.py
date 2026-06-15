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

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, replace

from . import ICD_VERSION, __version__
from .io_driver import (
    FAULT_CALIBRATION,
    FAULT_INPUT,
    FAULT_MODE_UNKNOWN,
    FAULT_OUTPUT,
    InputSnapshot,
    OutputState,
)
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

# Per ICD §6, each command has a permitted-mode set. Writes from a non-
# permitted mode are rejected (and the rejection latch sets FAULT_INPUT
# until the next valid command clears it).
_ALLOWED_MODES_FOR_ADDR: dict[int, frozenset[str]] = {
    ADDR_CMD_TEST: frozenset(["auto"]),
    ADDR_CMD_INHIBIT: frozenset(["auto", "manual"]),
    ADDR_CMD_FORCE_TRANSFER: frozenset(["auto"]),
    ADDR_CMD_BYPASS_DELAY: frozenset(["auto"]),
}

# Default pulse duration when a Modbus client sets cmd_test or cmd_bypass_delay
# without specifying one. Sits in the middle of the ICD §6.1 range.
DEFAULT_PULSE_MS = 750
ROLLING_WINDOW_S = 24 * 3600


# Fault-summary bit masks (ICD §5.1.1) are defined at the I/O boundary
# (``atspi.io_driver``) and imported above; they are re-exported from this
# module for the register store, the Modbus server, and the health decoder.

# ICD §5.1.1: bits 4-15 are RESERVED and MUST be 0 on the wire. A buggy
# driver that reports stray bits in InputSnapshot.fault_bits must not be
# able to leak them to GenWatch.
_FAULT_DEFINED_MASK = FAULT_INPUT | FAULT_OUTPUT | FAULT_MODE_UNKNOWN | FAULT_CALIBRATION

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

    # ICD §write response contract: latched when a mode-restricted write is
    # rejected; cleared by the next valid command. Surfaces in fault_summary
    # as FAULT_INPUT until cleared.
    mode_reject_active: bool = False


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
        # Monotonic boot timestamp. Wall-clock would break ICD §6.2's
        # "uptime is strictly increasing within a boot" guarantee on the
        # first NTP correction backward — GenWatch interprets a backward
        # jump as an undetected reboot.
        self._boot_mono = time.monotonic()
        self._state_file = state_file
        # Wallclock (UTC epoch seconds) of every transfer in the rolling
        # 24-hour window. Wallclock rather than monotonic so the deque
        # can survive across restarts via state_file. Eviction tolerates
        # small NTP corrections; a large clock step could mis-age entries
        # but won't crash anything.
        self._transfer_timestamps: deque[int] = deque()

        persisted = state_file.load() if state_file else PersistedState()
        # Reload the sliding window, evicting anything already past the
        # 24h cutoff at startup.
        now_wall = int(time.time())
        cutoff = now_wall - ROLLING_WINDOW_S
        self._transfer_timestamps.extend(
            int(ts) for ts in persisted.recent_transfer_wallclocks if int(ts) >= cutoff
        )
        self._snap = _StateSnapshot(
            last_transfer_to_gen_ts=persisted.last_transfer_to_gen_ts,
            last_retransfer_to_util_ts=persisted.last_retransfer_to_util_ts,
            transfer_count_lifetime=persisted.transfer_count_lifetime,
            transfer_count_24h=len(self._transfer_timestamps),
        )

    # ─── Sampling-loop writers ────────────────────────────────────────

    def apply_input_snapshot(self, inputs: InputSnapshot) -> None:
        """Apply a fresh input read. Atomic snapshot swap; transitions
        update counters and timestamps.
        """
        now_wall = int(time.time())
        persist = False

        with self._lock:
            prev = self._snap
            new_last_to_gen = prev.last_transfer_to_gen_ts
            new_last_to_util = prev.last_retransfer_to_util_ts
            new_lifetime = prev.transfer_count_lifetime

            # Count a transfer only when the new position is reached from a
            # position that legitimately precedes it. The valid predecessors
            # are the prior rail or the brief "transferring" hold (driven by
            # the Load Disconnect pulse during the stroke):
            #   • transfer-to-gen:   utility | transferring → generator
            #   • retransfer-to-util: generator | transferring → utility
            #
            # Excluding "unknown" as a predecessor matters two ways:
            #   1. Boot position defaults to "unknown", so a plain
            #      "unknown → generator" first read would otherwise increment
            #      transfer_count_lifetime on *every reboot* that happens while
            #      the ATS is sitting on the generator (e.g. during a utility
            #      outage) — the persisted count would drift up with reboots.
            #   2. A momentary both-aux-open glitch reads as "unknown"; the
            #      bounce back to the same rail would otherwise double-count.
            # Including "transferring" as a predecessor is what lets the
            # realistic gen → transferring → utility retransfer stamp
            # last_retransfer_to_util_ts at all (the Load Disconnect hold means
            # the position seen just before "utility" is "transferring", not
            # "generator").
            transferred_to_gen = (
                prev.position in ("utility", "transferring")
                and inputs.position == "generator"
            )
            retransferred_to_util = (
                prev.position in ("generator", "transferring")
                and inputs.position == "utility"
            )

            if transferred_to_gen:
                new_last_to_gen = now_wall
                new_lifetime = prev.transfer_count_lifetime + 1
                self._transfer_timestamps.append(now_wall)
                persist = True
            elif retransferred_to_util:
                new_last_to_util = now_wall
                persist = True

            self._evict_old_transfers(now_wall)

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
                # The mode-reject latch lives across sampling cycles — only
                # the next accepted command clears it (ICD §write response
                # contract).
                mode_reject_active=prev.mode_reject_active,
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

    def read_register(
        self,
        addr: int,
        *,
        now_mono: float | None = None,
        now_wall: int | None = None,
    ) -> int:
        """Return the 16-bit value at the given PDU address.

        ``now_mono`` and ``now_wall`` let a caller pin the time values
        used for derived registers (``uptime_s``, ``wallclock``) across
        a multi-word read. Without that, a two-word u32 read
        independently reads ``time.*`` twice — if the second call
        straddles a second boundary the high/low pair is inconsistent
        and GenWatch reconstructs a wrong value. The Modbus data block
        threads a single timestamp through every register in one
        ``getValues`` call; direct callers (tests, the health endpoint)
        can omit and accept a small race on the u32 registers.
        """
        if now_mono is None:
            now_mono = time.monotonic()
        if now_wall is None:
            now_wall = int(time.time())
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
                bits = s.fault_bits
                # Surface the mode-reject latch as FAULT_INPUT per ICD
                # §write response contract.
                if s.mode_reject_active:
                    bits |= FAULT_INPUT
                # ICD §5.1.1: reserved bits MUST be 0 on the wire.
                return bits & _FAULT_DEFINED_MASK

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
                up = int(now_mono - self._boot_mono)
                return (up >> 16) & 0xFFFF
            if addr == ADDR_UPTIME_S + 1:
                up = int(now_mono - self._boot_mono)
                return up & 0xFFFF
            if addr == ADDR_WALLCLOCK:
                return (now_wall >> 16) & 0xFFFF
            if addr == ADDR_WALLCLOCK + 1:
                return now_wall & 0xFFFF
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

    def can_write(self, addr: int) -> bool:
        """Pre-validate a holding-register write for the Modbus
        ``validate()`` hook. Returns ``True`` if a write to ``addr``
        is allowed under the current mode policy; ``False`` otherwise.

        A ``False`` return latches ``mode_reject_active`` so the
        next ``fault_summary`` read surfaces the rejection via
        ``FAULT_INPUT`` (ICD §write response contract). Unknown
        addresses always return ``False`` — the server already gates
        unknown writes via ``_ALLOWED_HOLDING_WRITE_ADDRESSES`` and
        does NOT consult this method, so an unknown-address call here
        is a logic error in the caller and we choose the safer answer.
        """
        allowed_modes = _ALLOWED_MODES_FOR_ADDR.get(addr)
        if allowed_modes is None:
            return False
        with self._lock:
            mode = self._snap.ats_mode
        if mode not in allowed_modes:
            with self._lock:
                self._snap = replace(self._snap, mode_reject_active=True)
            log.warning(
                "write to %#06x rejected: ats_mode=%s, allowed=%s",
                addr, mode, sorted(allowed_modes),
            )
            return False
        return True

    def write_register(self, addr: int, value: int) -> CommandIntent | None:
        """Translate a Modbus write into a driver-level command intent.

        Returns the :class:`CommandIntent` the server layer should
        dispatch to the I/O driver, or ``None`` if the write is
        unrecognized, mode-restricted, or has an invalid value. The
        store does NOT mutate read-back state itself — that happens
        once the next sampling cycle reads the driver's actual output
        state. This keeps the read-back register honest (it reflects
        what the relay actually is, not what we asked for).

        The Modbus server pre-validates mode policy via
        :meth:`can_write`, so under the production path a ``None``
        return from here is always value-validation failure. The
        in-method mode check is retained as defence-in-depth for
        callers that bypass ``can_write`` (unit tests, future
        non-Modbus front-ends).
        """
        # Step 1: is this a known command register at all?
        allowed_modes = _ALLOWED_MODES_FOR_ADDR.get(addr)
        if allowed_modes is None:
            return None

        # Step 2: per ICD §6, enforce the mode-permission policy. A reject
        # latches FAULT_INPUT until cleared by the next accepted command.
        with self._lock:
            mode = self._snap.ats_mode
        if mode not in allowed_modes:
            with self._lock:
                self._snap = replace(self._snap, mode_reject_active=True)
            log.warning(
                "write to %#06x rejected: ats_mode=%s, allowed=%s",
                addr, mode, sorted(allowed_modes),
            )
            return None

        # Step 3: value validation + intent construction.
        intent: CommandIntent | None = None
        if addr == ADDR_CMD_TEST and value == 0x0001:
            intent = CommandIntent(test_pulse_ms=DEFAULT_PULSE_MS)
        elif addr == ADDR_CMD_INHIBIT and value in (0x0000, 0x0001):
            intent = CommandIntent(inhibit=value == 0x0001)
        elif addr == ADDR_CMD_FORCE_TRANSFER and value in (0x0000, 0x0001):
            intent = CommandIntent(force_transfer=value == 0x0001)
        elif addr == ADDR_CMD_BYPASS_DELAY and value == 0x0001:
            intent = CommandIntent(bypass_delay_pulse_ms=DEFAULT_PULSE_MS)

        if intent is not None:
            # An accepted command clears the mode-reject latch (ICD).
            with self._lock:
                if self._snap.mode_reject_active:
                    self._snap = replace(self._snap, mode_reject_active=False)
        return intent

    # ─── Internal helpers ─────────────────────────────────────────────

    def _evict_old_transfers(self, now_wall: int) -> None:
        cutoff = now_wall - ROLLING_WINDOW_S
        while self._transfer_timestamps and self._transfer_timestamps[0] < cutoff:
            self._transfer_timestamps.popleft()

    def _persist_async_safe(self) -> None:
        """Snapshot persisted fields under the lock, then write outside.

        When running on an asyncio event loop (the production case), the
        actual file write (including fsync, which can stall 50–200 ms on
        a microSD) is offloaded to the loop's default executor so the
        10 Hz sampling loop is not blocked. Callers from a synchronous
        context (unit tests) get a synchronous save.

        Catches I/O errors so a flaky disk never crashes the service.
        """
        with self._lock:
            persisted = PersistedState(
                transfer_count_lifetime=self._snap.transfer_count_lifetime,
                last_transfer_to_gen_ts=self._snap.last_transfer_to_gen_ts,
                last_retransfer_to_util_ts=self._snap.last_retransfer_to_util_ts,
                recent_transfer_wallclocks=list(self._transfer_timestamps),
            )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._save_blocking(persisted)
            return
        loop.run_in_executor(None, self._save_blocking, persisted)

    def _save_blocking(self, persisted: PersistedState) -> None:
        try:
            self._state_file.save(persisted)
        except OSError as e:
            log.warning("failed to persist state: %s", e)
