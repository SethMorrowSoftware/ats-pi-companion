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

    def note_modbus_read(self) -> None:
        """Called by the Modbus server's data block on every successful
        read. Cheap and frequent; do nothing expensive here.
        """
        self._last_read_monotonic = time.monotonic()
        if self._released:
            # Comms recovered — re-arm
            log.info("comms recovered; watchdog re-armed")
            self._released = False

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
            if elapsed > TIMEOUT_S and not self._released:
                log.warning(
                    "Modbus comms silent for %.1fs (> %.1fs) — auto-releasing "
                    "maintained commands per ICD §8.3",
                    elapsed, TIMEOUT_S,
                )
                # Release in the store (so read-back registers reflect
                # the release immediately) AND drive the physical
                # release through the I/O layer.
                self._store.release_maintained_commands()
                try:
                    await self._driver.drive_outputs(
                        inhibit=False,
                        force_transfer=False,
                    )
                except Exception as e:  # noqa: BLE001
                    log.exception("safety watchdog: drive_outputs failed: %s", e)
                self._released = True
