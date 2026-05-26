"""CLI entry point: ``atspi --config /etc/atspi/config.yaml``.

Brings up the ATS-Pi service: I/O driver + sampling loop + Modbus TCP
server + safety watchdog. Runs until SIGTERM/SIGINT.
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
from .safety import SafetyWatchdog
from .server import start_server
from .state import RegisterStore

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
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("sampling cycle failed: %s", e)
            store.set_input_fault(True)
        await asyncio.sleep(0.1)


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

    store = RegisterStore(unit_id=cfg.site.unit_id)
    watchdog = SafetyWatchdog(store, driver)

    sample_task = asyncio.create_task(_sampling_loop(driver, store), name="sampling")
    watchdog_task = asyncio.create_task(watchdog.run(), name="safety-watchdog")
    server_task = await start_server(
        host=cfg.modbus_server.host,
        port=cfg.modbus_server.port,
        unit_id=cfg.modbus_server.unit_id,
        store=store,
        on_read=watchdog.note_modbus_read,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    log.info("atspi is running — Ctrl-C to stop")
    await stop.wait()

    log.info("atspi shutting down")
    for t in (sample_task, watchdog_task, server_task):
        if t is not None:
            t.cancel()
    for t in (sample_task, watchdog_task, server_task):
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
