"""Comms-loss safety watchdog (ICD §8.3).

If the ATS-Pi hasn't received a successful Modbus read from GenWatch
within the timeout window, automatically release any maintained
commands (inhibit, force-transfer). This is the critical safety rule
that prevents an operator's stale "force transfer" from leaving the
ATS in a manual state forever.

The watchdog runs as its own asyncio task. The Modbus server's
LiveDataBlock hook calls ``note_modbus_read()`` on every successful
read; the watchdog wakes every second and checks elapsed time.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .io_driver import IODriver
from .state import RegisterStore

log = logging.getLogger("atspi.safety")

# ICD §8.3 — 30 ± 5 s.
TIMEOUT_S = 30.0
CHECK_INTERVAL_S = 1.0


class SafetyWatchdog:
    """Auto-release maintained commands on Modbus comms timeout."""

    def __init__(self, store: RegisterStore, driver: IODriver):
        self._store = store
        self._driver = driver
        self._last_read_monotonic: float = time.monotonic()
        # Whether we've already fired the auto-release for the current
        # silence interval (one release per timeout event, not per
        # check tick).
        self._released: bool = False
        # Set when the *commanding* connection (GenWatch) drops — forces an
        # immediate release on the next tick rather than waiting out the full
        # silence window (ICD §9.1: a TCP drop is unambiguous comms loss).
        # Cleared by note_modbus_read when activity resumes.
        self._commander_gone: bool = False

    def note_modbus_read(self) -> None:
        """Called for a successful Modbus read FROM THE AUTHORITATIVE
        (GenWatch) connection — see server.ConnectionTracker, which scopes
        this so a diagnostic reader on a second connection can't keep the
        watchdog alive (ICD §3/§8.3). Cheap and frequent; nothing expensive.
        """
        self._last_read_monotonic = time.monotonic()
        self._commander_gone = False
        if self._released:
            # Comms recovered — re-arm
            log.info("comms recovered; watchdog re-armed")
            self._released = False

    def note_commander_lost(self) -> None:
        """Called when the authoritative GenWatch connection drops.

        A dropped TCP connection is unambiguous comms loss: the operator who
        asserted a maintained command can no longer release it remotely, so we
        must not wait out the full 30 s silence window. The next tick releases.
        Harmless no-op if nothing is asserted.
        """
        self._commander_gone = True

    def snapshot(self) -> tuple[float, bool]:
        """Return ``(seconds_since_last_modbus_read, released)``.

        Stable shape for callers (health endpoint, future metrics)
        that want to observe watchdog state without poking at private
        attributes.
        """
        return time.monotonic() - self._last_read_monotonic, self._released

    async def run(self) -> None:
        log.info(
            "safety watchdog running (timeout=%.1fs, check every %.1fs)",
            TIMEOUT_S, CHECK_INTERVAL_S,
        )
        while True:
            try:
                await asyncio.sleep(CHECK_INTERVAL_S)
            except asyncio.CancelledError:
                return
            elapsed = time.monotonic() - self._last_read_monotonic
            commander_gone = self._commander_gone
            if (elapsed > TIMEOUT_S or commander_gone) and not self._released:
                reason = (
                    "commanding connection dropped"
                    if commander_gone and elapsed <= TIMEOUT_S
                    else f"silent for {elapsed:.1f}s (> {TIMEOUT_S:.1f}s)"
                )
                log.warning(
                    "Modbus comms %s — auto-releasing maintained commands "
                    "per ICD §8.3",
                    reason,
                )
                # Release in the store (so read-back registers reflect
                # the release immediately) AND drive the physical
                # release through the I/O layer. Latch _released ONLY
                # when the physical write succeeded — otherwise an ADAM
                # blip during a comms-loss event would leave inhibit /
                # force-transfer asserted on the hardware until comms
                # came back. Re-tries every CHECK_INTERVAL_S until the
                # write lands.
                self._store.release_maintained_commands()
                try:
                    await self._driver.drive_outputs(
                        inhibit=False,
                        force_transfer=False,
                    )
                except Exception as e:  # noqa: BLE001
                    log.exception(
                        "safety watchdog: drive_outputs failed (%s); "
                        "will retry next tick",
                        e,
                    )
                    continue
                self._released = True
