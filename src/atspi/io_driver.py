"""Abstract I/O driver interface — the boundary between the ATS-Pi
service and the physical world.

Two concrete impls expected: ``io_mock`` (for dev, CI, integration
testing) and ``io_adam`` (for production with an Advantech ADAM-6060).

Both implementations MUST honor the contract described here:

  - read_inputs() returns the six core-state inputs in a single atomic
    snapshot (no torn reads — see ICD §8.1)
  - drive_outputs() is idempotent; calling it multiple times with the
    same state is safe
  - Pulsed outputs (test, bypass_delay) self-clear after the requested
    duration; the driver owns the timer
  - All methods are async and MUST NOT block the event loop for more
    than 100 ms (sampling loop runs at 10 Hz)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# Fault-summary bit masks (ICD §5.1.1). Defined here at the I/O boundary
# because drivers populate ``InputSnapshot.fault_bits``; re-exported from
# ``atspi.state`` for the register store, Modbus server, and health decoder.
FAULT_INPUT = 0x0001
FAULT_OUTPUT = 0x0002
FAULT_MODE_UNKNOWN = 0x0004
FAULT_CALIBRATION = 0x0008


@dataclass
class InputSnapshot:
    """One coherent read of all six core input contacts."""
    # 'utility' | 'generator' | 'transferring' | 'unknown'
    position: str
    normal_available: bool
    emergency_available: bool
    engine_start_calling: bool
    # ATS mode if the driver can determine it; otherwise 'unknown'.
    ats_mode: str = "auto"
    # Fault bits (ICD §5.1.1) detected at the I/O layer.
    fault_bits: int = 0


@dataclass
class OutputState:
    """Mirror of the currently-driven relay outputs (ICD §5.5)."""
    test_active: bool
    inhibit_active: bool
    force_transfer_active: bool
    bypass_delay_active: bool


class IODriver(Protocol):
    """All physical I/O happens through this. Implementations:
       - ``IOMockDriver`` (atspi.io_mock) — in-memory, programmable
       - ``IOAdamDriver`` (atspi.io_adam) — ADAM-6060 over Modbus TCP
    """

    async def connect(self) -> bool:
        """Open hardware/network connections. Idempotent.

        Returns True on success. Returning False does NOT raise — the
        service starts in a degraded state and the sampling loop keeps
        retrying via subsequent calls (similar to GenWatch's
        connect-during-startup behaviour).
        """
        ...

    async def close(self) -> None:
        """Tear down. Idempotent."""
        ...

    async def read_inputs(self) -> InputSnapshot:
        """Read all six core input contacts as one atomic snapshot.

        Raises on connection failures; the caller (sampling loop)
        catches and sets a fault bit.
        """
        ...

    async def read_output_state(self) -> OutputState:
        """Read back the currently-asserted outputs for the ICD §5.5
        mirror registers.
        """
        ...

    async def drive_outputs(
        self,
        *,
        test_pulse_ms: int | None = None,
        inhibit: bool | None = None,
        force_transfer: bool | None = None,
        bypass_delay_pulse_ms: int | None = None,
    ) -> None:
        """Apply commanded outputs. ``None`` for any parameter means
        "leave unchanged". Pulsed outputs (``test_pulse_ms``,
        ``bypass_delay_pulse_ms``) are self-clearing — the driver
        schedules the release internally.

        Pulse durations are clamped to the ICD §6.1 range (500-1500 ms)
        by the driver to enforce the contract on the wire.
        """
        ...

    async def release_all_outputs(self) -> None:
        """Drive every ATS command output to its released (OFF) state —
        the maintained pair (inhibit, force_transfer) AND the pulsed pair
        (test, bypass_delay) — cancelling any in-flight pulse timers.

        Called by the orchestrator at service startup (ICD §9.3: command
        registers MUST start in the no-commands-asserted state after a
        reboot, even if a previous instance died with a relay latched and
        the restart was fast enough to beat the ADAM's host-idle watchdog),
        at graceful shutdown (so a ``systemctl stop``/``restart`` doesn't
        leave a relay latched), and by the bench cleanup path.

        This is a release, so it MUST be permitted even while the F1
        hardware fail-safe is unverified — same rule as ``drive_outputs``
        with ``False`` values. Raises on I/O failure; callers retry or
        fall back to the ADAM hardware watchdog.
        """
        ...

    def check_output_consistency(self, actual: OutputState) -> bool:
        """Compare the just-read driver state to what we last commanded.

        Returns True if the driven outputs match the commanded state (or
        if we're within a settling window after a recent write — relays
        take time to physically respond). Returns False if a stuck relay
        is suspected: e.g. we commanded ``inhibit=True``, settling window
        has elapsed, and the read-back still shows it off.

        Drivers that can't observe a separate read-back path (the mock,
        any driver where the read literally returns what was written)
        return True unconditionally — there's nothing to verify.
        """
        ...

    def hw_watchdog_ok(self) -> bool:
        """Whether the hardware host-watchdog fail-safe is verified armed (F1).

        For a real ADAM this reflects the connect-time readback of the host
        watchdog / DO safety-value config; while it is False the driver itself
        refuses to assert outputs (the enforcement that actually blocks the
        relay is local to this service) and the sampling loop publishes a
        persistent OUTPUT_FAULT, which GenWatch surfaces as a fault alarm.
        Drivers with no such fail-safe to verify (the mock) return True —
        nothing to gate.
        """
        ...

    def hw_watchdog_status(self) -> tuple[bool, str]:
        """``(hw_watchdog_ok, human_readable_detail)`` for startup logging and
        health reporting. The detail explains *why* the fail-safe is (not)
        considered armed.
        """
        ...
