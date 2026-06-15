"""Hybrid I/O driver — serial monitoring + ADAM control.

Splits the I/O contract across the two interfaces that an install with
only the Pi + ADAM-6060 + ASCO (no 18RX, no 14AA/14BA aux contacts)
actually has:

  read_inputs()        → ASCO Group 5 controller over RS-485 Modbus RTU
                         (AscoSerialReader) — position + source availability
  drive_outputs() etc. → ADAM-6060 relays on ASCO terminals 6-13
                         (IOAdamDriver) — Test / Inhibit / Force-Transfer /
                         Bypass, with the F1 hardware fail-safe intact

Why this split (HARDWARE.md §3.1): the ASCO's 16-terminal customer strip
gives the ADAM only one of the six sense inputs (Load Disconnect), so the
contact-only path needs ~$800 of accessories (18RX + aux contacts) to read
position and source availability. The serial link reads all of that
straight from the controller for the price of a USB-RS485 adapter. Control
stays on the ADAM because its relays drive documented dry-contact inputs
and — critically — the ADAM's host-idle watchdog is the hardware fail-safe
that releases a latched relay if the Pi dies. ASCO serial *write* support
is firmware-dependent and would need its own safety story, so commanding is
deliberately left on the proven ADAM path.

Every output-side method (including the F1 ``hw_watchdog_*`` gate and
stuck-relay ``check_output_consistency``) delegates to the ADAM driver
unchanged, so the safety behaviour is identical to a ``driver: adam``
deployment — only the read path differs.
"""
from __future__ import annotations

import logging

from .io_adam import IOAdamDriver
from .io_asco_serial import AscoSerialReader
from .io_driver import InputSnapshot, OutputState

log = logging.getLogger("atspi.io_hybrid")


class IOHybridDriver:
    """Compose an ASCO serial reader (inputs) with an ADAM driver (outputs)."""

    def __init__(self, reader: AscoSerialReader, outputs: IOAdamDriver):
        self._reader = reader
        self._outputs = outputs

    async def connect(self) -> bool:
        # Connect the control path first — it carries the safety-critical F1
        # watchdog readback. Both sub-drivers reconnect lazily per-operation,
        # so a failure here is non-fatal: the orchestrator logs it and the
        # sampling loop recovers (see __main__ run()).
        out_ok = await self._outputs.connect()
        in_ok = await self._reader.connect()
        if not out_ok:
            log.warning("hybrid: ADAM control path not connected at startup")
        if not in_ok:
            log.warning("hybrid: ASCO serial monitoring path not connected at startup")
        return out_ok and in_ok

    async def close(self) -> None:
        # Close both; never let one failure skip the other.
        try:
            await self._reader.close()
        finally:
            await self._outputs.close()

    # ── Inputs: ASCO Group 5 over serial ──
    async def read_inputs(self) -> InputSnapshot:
        return await self._reader.read_inputs()

    # ── Outputs + safety: ADAM-6060, delegated unchanged ──
    async def read_output_state(self) -> OutputState:
        return await self._outputs.read_output_state()

    async def drive_outputs(
        self,
        *,
        test_pulse_ms: int | None = None,
        inhibit: bool | None = None,
        force_transfer: bool | None = None,
        bypass_delay_pulse_ms: int | None = None,
    ) -> None:
        await self._outputs.drive_outputs(
            test_pulse_ms=test_pulse_ms,
            inhibit=inhibit,
            force_transfer=force_transfer,
            bypass_delay_pulse_ms=bypass_delay_pulse_ms,
        )

    async def release_all_outputs(self) -> None:
        await self._outputs.release_all_outputs()

    def check_output_consistency(self, actual: OutputState) -> bool:
        return self._outputs.check_output_consistency(actual)

    def hw_watchdog_ok(self) -> bool:
        return self._outputs.hw_watchdog_ok()

    def hw_watchdog_status(self) -> tuple[bool, str]:
        return self._outputs.hw_watchdog_status()


__all__ = ["IOHybridDriver"]
