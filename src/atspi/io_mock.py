"""Mock I/O driver — in-memory contact state, no hardware required.

Used for development and integration testing without an ADAM-6060.
State can be flipped programmatically (for unit tests) or via signals
sent to the running service:

  - ``SIGUSR1`` cycles the position through utility → generator →
    transferring → unknown → (back to utility).
  - ``SIGUSR2`` toggles normal_available; engine_start_calling mirrors
    the inverted value, matching how the ASCO actually behaves.

The signal handlers are installed inside :meth:`connect` so they're
only registered when the mock is the active driver running on an
asyncio event loop.

Defaults represent a healthy steady-state: load on Normal, both sources
available, AUTO mode, no faults.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from .io_driver import InputSnapshot, OutputState

log = logging.getLogger("atspi.io_mock")

_POSITION_CYCLE = ("utility", "generator", "transferring", "unknown")


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
        self._install_signal_handlers()
        return True

    async def close(self) -> None:
        for t in (self._test_release_task, self._bypass_release_task):
            if t is not None:
                t.cancel()
        self._remove_signal_handlers()

    # ── Runtime control via signals ──────────────────────────────────

    def _install_signal_handlers(self) -> None:
        """Wire SIGUSR1 / SIGUSR2 onto the running event loop. Quiet
        no-op when not on an event loop (most unit tests) or when the
        platform doesn't support signal handlers (Windows).
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            loop.add_signal_handler(signal.SIGUSR1, self.cycle_position)
            loop.add_signal_handler(signal.SIGUSR2, self.toggle_normal_available)
        except (NotImplementedError, OSError, ValueError):
            return
        log.info(
            "mock: SIGUSR1 cycles position (utility→generator→transferring→unknown), "
            "SIGUSR2 toggles normal_available + engine_start"
        )

    def _remove_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for sig in (signal.SIGUSR1, signal.SIGUSR2):
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, OSError, ValueError):
                pass

    def cycle_position(self) -> None:
        """Advance position one step around _POSITION_CYCLE. Bound to
        SIGUSR1 when running on a loop; also directly callable from tests.
        """
        try:
            idx = _POSITION_CYCLE.index(self.position)
        except ValueError:
            idx = -1
        self.position = _POSITION_CYCLE[(idx + 1) % len(_POSITION_CYCLE)]
        log.info("mock SIGUSR1: position → %s", self.position)

    def toggle_normal_available(self) -> None:
        """Flip normal_available and mirror the ASCO behaviour where
        engine_start_calling asserts when utility is lost. Bound to
        SIGUSR2; also directly callable from tests.
        """
        self.normal_available = not self.normal_available
        self.engine_start_calling = not self.normal_available
        log.info(
            "mock SIGUSR2: normal_available=%s engine_start=%s",
            self.normal_available, self.engine_start_calling,
        )

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

    def hw_watchdog_ok(self) -> bool:
        # The mock has no hardware host-watchdog fail-safe to verify, so there is
        # nothing to gate (F1). Always "armed" — same posture as
        # check_output_consistency.
        return True

    def hw_watchdog_status(self) -> tuple[bool, str]:
        return True, "no hardware fail-safe to verify (mock driver)"

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

    async def release_all_outputs(self) -> None:
        for t in (self._test_release_task, self._bypass_release_task):
            if t is not None and not t.done():
                t.cancel()
        self._test_active = False
        self._inhibit_active = False
        self._force_transfer_active = False
        self._bypass_delay_active = False
        log.info("mock: all command outputs released")

    async def _pulse(self, which: str, duration_ms: int) -> None:
        # ICD §6: writes during an active pulse are IGNORED — the original
        # pulse runs to its scheduled completion without being re-triggered
        # or extended. This prevents a flapping caller from stacking pulses.
        if which == "test":
            if self._test_active:
                log.debug("mock: cmd_test already active; ignoring re-trigger")
                return
            self._test_active = True
        else:  # bypass
            if self._bypass_delay_active:
                log.debug("mock: cmd_bypass_delay already active; ignoring re-trigger")
                return
            self._bypass_delay_active = True

        # Clamp to ICD §6.1 (500-1500 ms)
        ms = max(500, min(1500, int(duration_ms)))
        if which == "test":
            self._test_release_task = asyncio.create_task(self._release("test", ms))
        else:
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
