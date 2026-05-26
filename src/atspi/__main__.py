"""CLI entry point: ``atspi --config /etc/atspi/config.yaml``.

Brings up the ATS-Pi service: I/O driver + sampling loop + Modbus TCP
server + safety watchdog + command dispatcher. Runs until SIGTERM/SIGINT.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from . import __version__
from .config import load_config
from .io_driver import IODriver
from .io_mock import IOMockDriver
from .notify import ready as notify_ready
from .notify import watchdog_loop as notify_watchdog_loop
from .persistence import StateFile
from .safety import SafetyWatchdog
from .server import start_server
from .state import CommandIntent, RegisterStore

# Half of systemd's WatchdogSec=60 — gives plenty of margin while still
# catching a hung event loop within a minute.
SYSTEMD_WATCHDOG_PING_S = 30.0

log = logging.getLogger("atspi")


def _build_io_driver(cfg) -> IODriver:
    """Construct the configured I/O driver. Defaults to mock when no
    real-hardware driver is configured — keeps dev easy and prevents a
    crash on missing hardware credentials.
    """
    driver_name = cfg.io.driver
    if driver_name == "mock":
        return IOMockDriver()
    if driver_name == "adam":
        # Lazy import — pulls in pymodbus client, not needed for mock-only dev
        from .io_adam import IOAdamDriver
        return IOAdamDriver(
            host=cfg.io.adam.host,
            port=cfg.io.adam.port,
            unit_id=cfg.io.adam.unit_id,
        )
    raise ValueError(f"unknown io.driver: {driver_name!r}")


async def _sampling_loop(driver: IODriver, store: RegisterStore) -> None:
    """10 Hz input/output read loop. Atomic snapshot publication to the
    store; exceptions caught and reported as fault bits.
    """
    log.info("sampling loop starting at 10 Hz")
    while True:
        try:
            inputs = await driver.read_inputs()
            outputs = await driver.read_output_state()
            store.apply_input_snapshot(inputs)
            store.apply_output_state(outputs)
            store.set_input_fault(False)
            # Stuck-relay detection: compare actual driver state against
            # the last commanded state. The driver enforces its own
            # settling window so a write that hasn't physically actuated
            # yet isn't flagged. OUTPUT_FAULT stays set until cleared by
            # the next successful drive_outputs() in _dispatch_command.
            if not driver.check_output_consistency(outputs):
                store.set_output_fault(True)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("sampling cycle failed: %s", e)
            store.set_input_fault(True)
        await asyncio.sleep(0.1)


async def _dispatch_command(driver: IODriver, store: RegisterStore, intent: CommandIntent) -> None:
    """Apply a recognized Modbus write to the physical driver."""
    try:
        await driver.drive_outputs(
            test_pulse_ms=intent.test_pulse_ms,
            inhibit=intent.inhibit,
            force_transfer=intent.force_transfer,
            bypass_delay_pulse_ms=intent.bypass_delay_pulse_ms,
        )
        store.set_output_fault(False)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("command dispatch failed (intent=%s): %s", intent, e)
        store.set_output_fault(True)


async def _amain(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log.info("atspi v%s starting (unit_id=%d)", __version__, cfg.site.unit_id)

    driver = _build_io_driver(cfg)
    connected = await driver.connect()
    if not connected:
        log.error("I/O driver failed to connect; will keep retrying in sampling loop")

    state_file = StateFile(cfg.persistence.state_file)
    store = RegisterStore(unit_id=cfg.site.unit_id, state_file=state_file)
    watchdog = SafetyWatchdog(store, driver)

    loop = asyncio.get_running_loop()

    def on_command(intent: CommandIntent) -> None:
        # setValues is called from the pymodbus async server task on
        # this same loop, so create_task is safe.
        loop.create_task(_dispatch_command(driver, store, intent))

    sample_task = asyncio.create_task(_sampling_loop(driver, store), name="sampling")
    watchdog_task = asyncio.create_task(watchdog.run(), name="safety-watchdog")
    notify_task = asyncio.create_task(
        notify_watchdog_loop(SYSTEMD_WATCHDOG_PING_S), name="sd-watchdog"
    )
    server_task = await start_server(
        host=cfg.modbus_server.host,
        port=cfg.modbus_server.port,
        unit_id=cfg.modbus_server.unit_id,
        store=store,
        on_read=watchdog.note_modbus_read,
        on_command=on_command,
    )

    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    notify_ready()
    log.info("atspi is running — Ctrl-C to stop")
    await stop.wait()

    log.info("atspi shutting down")
    tasks = (sample_task, watchdog_task, notify_task, server_task)
    for t in tasks:
        if t is not None:
            t.cancel()
    for t in tasks:
        if t is None:
            continue
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    await driver.close()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="atspi", description="ATS-Pi companion service")
    ap.add_argument("--config", required=True, help="Path to config.yaml")
    ap.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    ap.add_argument("--version", action="version", version=f"atspi {__version__}")
    args = ap.parse_args()
    sys.exit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
