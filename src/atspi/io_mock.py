"""Mock I/O driver — in-memory contact state, no hardware required.

Used for development and integration testing without an ADAM-6060.
State can be flipped programmatically (for unit tests) or via the
process's stdin (when running interactively — see __main__.py for the
CLI hook).

Defaults represent a healthy steady-state: load on Normal, both sources
available, AUTO mode, no faults.
"""
from __future__ import annotations

import asyncio
import logging

from .io_driver import InputSnapshot, OutputState

log = logging.getLogger("atspi.io_mock")


class IOMockDriver:
    """Programmable in-memory I/O driver for development and testing."""

    def __init__(self) -> None:
        # Core state (defaults: healthy)
        self.position: str = "utility"
        self.normal_available: bool = True
        self.emergency_available: bool = True
        self.engine_start_calling: bool = False
        self.ats_mode: str = "auto"
        self.fault_bits: int = 0

        # Output state (defaults: nothing asserted)
        self._test_active: bool = False
        self._inhibit_active: bool = False
        self._force_transfer_active: bool = False
        self._bypass_delay_active: bool = False

        # Tasks holding pulsed-output release timers
        self._test_release_task: asyncio.Task | None = None
        self._bypass_release_task: asyncio.Task | None = None

    async def connect(self) -> bool:
        log.info("mock I/O driver connected (no hardware)")
        return True

    async def close(self) -> None:
        for t in (self._test_release_task, self._bypass_release_task):
            if t is not None:
                t.cancel()

    async def read_inputs(self) -> InputSnapshot:
        return InputSnapshot(
            position=self.position,
            normal_available=self.normal_available,
            emergency_available=self.emergency_available,
            engine_start_calling=self.engine_start_calling,
            ats_mode=self.ats_mode,
            fault_bits=self.fault_bits,
        )

    async def read_output_state(self) -> OutputState:
        return OutputState(
            test_active=self._test_active,
            inhibit_active=self._inhibit_active,
            force_transfer_active=self._force_transfer_active,
            bypass_delay_active=self._bypass_delay_active,
        )

    def check_output_consistency(self, actual: OutputState) -> bool:
        # Mock writes its own state back unchanged — there is no separate
        # read-back path, so nothing to verify. Always consistent.
        return True

    async def drive_outputs(
        self,
        *,
        test_pulse_ms: int | None = None,
        inhibit: bool | None = None,
        force_transfer: bool | None = None,
        bypass_delay_pulse_ms: int | None = None,
    ) -> None:
        if test_pulse_ms is not None:
            await self._pulse("test", test_pulse_ms)
        if inhibit is not None:
            self._inhibit_active = bool(inhibit)
            log.info("mock: inhibit %s", "ASSERT" if self._inhibit_active else "RELEASE")
        if force_transfer is not None:
            self._force_transfer_active = bool(force_transfer)
            log.info("mock: force_transfer %s", "ASSERT" if self._force_transfer_active else "RELEASE")
        if bypass_delay_pulse_ms is not None:
            await self._pulse("bypass", bypass_delay_pulse_ms)

    async def _pulse(self, which: str, duration_ms: int) -> None:
        # Clamp to ICD §6.1 (500-1500 ms)
        ms = max(500, min(1500, int(duration_ms)))
        if which == "test":
            self._test_active = True
            if self._test_release_task is not None:
                self._test_release_task.cancel()
            self._test_release_task = asyncio.create_task(self._release("test", ms))
        else:  # bypass
            self._bypass_delay_active = True
            if self._bypass_release_task is not None:
                self._bypass_release_task.cancel()
            self._bypass_release_task = asyncio.create_task(self._release("bypass", ms))
        log.info("mock: pulsing %s for %d ms", which, ms)

    async def _release(self, which: str, after_ms: int) -> None:
        try:
            await asyncio.sleep(after_ms / 1000.0)
            if which == "test":
                self._test_active = False
            else:
                self._bypass_delay_active = False
            log.info("mock: pulsed %s released", which)
        except asyncio.CancelledError:
            pass

    # ── Programmatic state-flip helpers for tests ────────────────────

    def set_normal_available(self, available: bool) -> None:
        self.normal_available = available
        # Mirror the ATS's typical behaviour — engine-start asserted
        # when utility is lost.
        self.engine_start_calling = not available

    def set_position(self, position: str) -> None:
        if position not in ("utility", "generator", "transferring", "unknown"):
            raise ValueError(position)
        self.position = position
