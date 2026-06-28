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
import time

from . import __version__
from .config import ConfigError, load_config
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

# Sampling loop cadence (10 Hz) and how often to re-log a sustained I/O
# failure. A hard ADAM/network outage fails every cycle; logging each one
# would emit ~10 lines/s and bury everything else in the journal, so the
# first failure is logged loudly and repeats are throttled to this cadence.
SAMPLE_INTERVAL_S = 0.1
SAMPLING_FAILURE_REMINDER_S = 30.0

log = logging.getLogger("atspi")

# The default site.unit_id — also the ADAM's factory-default address — is 1.
# A real deployment assigns each ATS-Pi a distinct id that GenWatch pins via
# its expected_unit_id check (ICD §5.4). Left at the default, register 0x0035
# reports 1 and GenWatch refuses authority — which surfaces as a confusing
# "authority refused" rather than an obvious misconfiguration.
_DEFAULT_SITE_UNIT_ID = 1


def _enforce_hw_watchdog_waiver(cfg) -> None:
    """Refuse to start if the F1 fail-safe is waived without an explicit ack.

    ``require_hw_watchdog: false`` removes the software readback gate; if the
    Pi-level hardware watchdog is also absent, a process death with a relay
    latched has no automatic release. A one-line waiver should not silently
    take on that risk, so we require a second, deliberate acknowledgement key.
    """
    if cfg.io.driver not in ("adam", "hybrid"):
        return
    if cfg.io.adam.require_hw_watchdog:
        return
    if cfg.io.adam.i_understand_no_crash_backstop:
        return
    raise ConfigError(
        "io.adam.require_hw_watchdog is false (F1 hardware fail-safe waived) "
        "but io.adam.i_understand_no_crash_backstop is not set. With the "
        "readback gate waived there is no automatic release if this Pi dies "
        "with a relay latched — only the §5.1 cable-pull test. To proceed, "
        "either set require_hw_watchdog: true (recommended) or explicitly set "
        "i_understand_no_crash_backstop: true to accept the procedural-only "
        "fail-safe. See HARDWARE.md §5."
    )


def _warn_if_default_unit_id(cfg) -> None:
    """Warn at startup if site.unit_id was left at the default.

    Non-fatal: a single bench/dev unit may legitimately run on 1, so this is
    a loud warning rather than a hard failure.
    """
    if cfg.site.unit_id == _DEFAULT_SITE_UNIT_ID:
        log.warning(
            "site.unit_id is %d (the default); register 0x0035 will report %d. "
            "GenWatch pins this via expected_unit_id (ICD §5.4) and will refuse "
            "authority unless they match. Set site.unit_id in your config "
            "(see config.example.yaml).",
            _DEFAULT_SITE_UNIT_ID, _DEFAULT_SITE_UNIT_ID,
        )


def _build_io_driver(cfg) -> IODriver:
    """Construct the configured I/O driver. Defaults to mock when no
    real-hardware driver is configured — keeps dev easy and prevents a
    crash on missing hardware credentials.
    """
    driver_name = cfg.io.driver
    if driver_name == "mock":
        return IOMockDriver()
    if driver_name == "adam":
        return _build_adam_driver(cfg)
    if driver_name == "hybrid":
        # Monitoring over serial (ASCO Group 5 Modbus RTU), control over the
        # ADAM. The ADAM half — including the F1 hardware fail-safe — is built
        # exactly as for driver: adam; only the read path is replaced.
        from .io_asco_serial import AscoSerialConfig, AscoSerialReader
        from .io_hybrid import IOHybridDriver
        s = cfg.io.asco_serial
        reader = AscoSerialReader(
            AscoSerialConfig(
                port=s.port,
                baudrate=s.baudrate,
                bytesize=s.bytesize,
                parity=s.parity,
                stopbits=s.stopbits,
                unit_id=s.unit_id,
                timeout_s=s.timeout_s,
                assumed_mode=s.assumed_mode,
                status_register=s.status_register,
                status_register_count=s.status_register_count,
                on_normal_bit=s.on_normal_bit,
                on_emergency_bit=s.on_emergency_bit,
                normal_available_bit=s.normal_available_bit,
                emergency_available_bit=s.emergency_available_bit,
                transferring_bit=s.transferring_bit,
                engine_start_bit=s.engine_start_bit,
            )
        )
        return IOHybridDriver(reader=reader, outputs=_build_adam_driver(cfg))
    raise ValueError(f"unknown io.driver: {driver_name!r}")


def _build_adam_driver(cfg):
    """Construct the ADAM-6060 driver from cfg.io.adam. Shared by the 'adam'
    and 'hybrid' drivers (hybrid uses it for the control/output path).
    """
    # Lazy import — pulls in pymodbus client, not needed for mock-only dev
    from .io_adam import HwWatchdogConfig, IOAdamDriver
    wd = cfg.io.adam.hw_watchdog
    return IOAdamDriver(
        host=cfg.io.adam.host,
        port=cfg.io.adam.port,
        unit_id=cfg.io.adam.unit_id,
        debounce_samples=cfg.io.adam.debounce_samples,
        assumed_mode=cfg.io.adam.assumed_mode,
        di_read=cfg.io.adam.di_read,
        require_hw_watchdog=cfg.io.adam.require_hw_watchdog,
        hw_watchdog=HwWatchdogConfig(
            enable_register=wd.enable_register,
            enable_expected=wd.enable_expected,
            timeout_register=wd.timeout_register,
            timeout_scale_s=wd.timeout_scale_s,
            timeout_min_s=wd.timeout_min_s,
            timeout_max_s=wd.timeout_max_s,
            safety_value_register_base=wd.safety_value_register_base,
            safety_value_count=wd.safety_value_count,
        ),
    )


async def _sampling_loop(driver: IODriver, store: RegisterStore) -> None:
    """10 Hz input/output read loop. Atomic snapshot publication to the
    store; exceptions caught and reported as fault bits.
    """
    log.info("sampling loop starting at 10 Hz")
    consecutive_failures = 0
    last_failure_log_mono = 0.0
    outputs_reset = False
    while True:
        try:
            # ICD §9.3: command outputs MUST start released after a service
            # (re)start. A fast restart can beat the ADAM's host-idle
            # watchdog, so a relay latched by the previous instance (or by a
            # stray bench write) would otherwise survive into this one with
            # nothing left to release it. One-shot, but retried every cycle
            # until the write lands (e.g. ADAM unreachable at boot).
            if not outputs_reset:
                await driver.release_all_outputs()
                outputs_reset = True
                log.info("startup: ATS command outputs reset to released (ICD §9.3)")
            inputs = await driver.read_inputs()
            outputs = await driver.read_output_state()
            store.apply_input_snapshot(inputs)
            store.apply_output_state(outputs)
            store.set_input_fault(False)
            # F1: an unverified hardware fail-safe (ADAM host-watchdog / DO
            # safety values not confirmed armed) is a persistent OUTPUT_FAULT —
            # re-asserted every cycle so GenWatch keeps seeing a non-authoritative
            # ATS link and refuses to command. Checked first so it isn't masked
            # by a transient clear from a release command in _dispatch_command.
            # Stuck-relay detection: compare actual driver state against the last
            # commanded state. The driver enforces its own settling window so a
            # write that hasn't physically actuated yet isn't flagged. OUTPUT_FAULT
            # stays set until cleared by the next successful drive_outputs().
            if not driver.hw_watchdog_ok() or not driver.check_output_consistency(outputs):
                store.set_output_fault(True)
            if consecutive_failures:
                log.info(
                    "sampling recovered after %d failed cycle(s) (~%.0fs)",
                    consecutive_failures, consecutive_failures * SAMPLE_INTERVAL_S,
                )
                consecutive_failures = 0
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            store.set_input_fault(True)
            consecutive_failures += 1
            now = time.monotonic()
            # Log the first failure of a streak loudly; throttle repeats so a
            # sustained outage doesn't flood the journal at 10 lines/s.
            if consecutive_failures == 1:
                log.warning("sampling cycle failed (%s): %s", type(e).__name__, e)
                last_failure_log_mono = now
            elif now - last_failure_log_mono >= SAMPLING_FAILURE_REMINDER_S:
                log.warning(
                    "sampling still failing after %d cycles (~%.0fs): %s",
                    consecutive_failures, consecutive_failures * SAMPLE_INTERVAL_S, e,
                )
                last_failure_log_mono = now
        await asyncio.sleep(SAMPLE_INTERVAL_S)


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
    # Configure logging FIRST so config-load errors and any messages from
    # driver/store construction land in the right place.
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    cfg = load_config(args.config)
    log.info("atspi v%s starting (unit_id=%d)", __version__, cfg.site.unit_id)
    _enforce_hw_watchdog_waiver(cfg)
    _warn_if_default_unit_id(cfg)

    driver = _build_io_driver(cfg)
    connected = await driver.connect()
    if not connected:
        log.error("I/O driver failed to connect; will keep retrying in sampling loop")
    # F1: surface the hardware fail-safe self-check at startup. While it is not
    # armed the driver refuses to assert outputs (the enforcement that blocks
    # the relay is local to this service) and the sampling loop publishes a
    # persistent OUTPUT_FAULT, which GenWatch surfaces as a fault alarm.
    hw_ok, hw_detail = driver.hw_watchdog_status()
    if not hw_ok:
        log.error(
            "ADAM hardware fail-safe NOT verified — %s. Outputs will be REFUSED "
            "and a persistent OUTPUT_FAULT published until this is resolved. See "
            "HARDWARE.md §5.1 (cable-pull test).",
            hw_detail,
        )
    elif cfg.io.driver in ("adam", "hybrid") and not cfg.io.adam.require_hw_watchdog:
        # The readback gate is waived (e.g. the ADAM-6060 can't expose its
        # FSV/WDT over Modbus), so nothing in software verifies the fail-safe.
        # Make the waiver loud rather than silent: F1 is now procedural.
        log.warning(
            "ADAM hardware fail-safe self-check is WAIVED (require_hw_watchdog: "
            "false). F1 protection is PROCEDURAL — the §5.1 cable-pull test is "
            "the only proof a latched relay releases on Pi death. Re-run it after "
            "any ADAM swap or factory reset (HARDWARE.md §5.2).",
        )

    state_file = StateFile(cfg.persistence.state_file)
    store = RegisterStore(unit_id=cfg.site.unit_id, state_file=state_file)
    # F1: publish the OUTPUT_FAULT before the server accepts its first read, so
    # a GenWatch poll in the gap before the first sampling tick can't briefly
    # see an authoritative link. The sampling loop then keeps it asserted.
    if not hw_ok:
        store.set_output_fault(True)
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
        # Scope the comms-loss watchdog to GenWatch's (the commanding)
        # connection rather than re-arming on any client's reads (C-1, §8.3).
        watchdog=watchdog,
        on_command=on_command,
    )

    health_server = None
    if cfg.health.enabled:
        from .health import start_health_server
        try:
            health_server = start_health_server(
                cfg.health.host, cfg.health.port, store, watchdog,
            )
        except OSError as e:
            log.error("health endpoint failed to start on %s:%d: %s",
                      cfg.health.host, cfg.health.port, e)

    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    # Tasks that MUST run for the service to be operational. If any of these
    # exits — for any reason — the service is degraded and systemd should
    # restart us. The notify task is excluded: it returns cleanly when not
    # under systemd, which is normal.
    critical_tasks: list[asyncio.Task] = [sample_task, watchdog_task, server_task]

    notify_ready()
    log.info("atspi is running — Ctrl-C to stop")

    reason = await _wait_for_shutdown_or_failure(stop, critical_tasks)
    if reason == "shutdown":
        log.info("atspi shutting down")
    else:
        log.error("atspi shutting down due to failure of critical task: %s", reason)

    if health_server is not None:
        health_server.stop()
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
    # Release the command outputs on the way out (same ICD §9.3 posture as
    # the startup reset): a stopped service must not leave a relay latched.
    # With the ADAM hardware fail-safe armed this merely beats the 5-10 s
    # host-idle release; on a bench module without it, it's the only release.
    try:
        await asyncio.wait_for(driver.release_all_outputs(), timeout=5.0)
        log.info("shutdown: ATS command outputs released")
    except Exception as e:  # noqa: BLE001
        log.warning(
            "shutdown: could not release ATS command outputs (%s) — the ADAM "
            "host-watchdog fail-safe (HARDWARE.md §5.1) is the remaining backstop",
            e,
        )
    await driver.close()
    # Non-zero exit code on critical-task failure so systemd's Restart=on-failure
    # kicks in immediately rather than waiting for the WatchdogSec timeout.
    return 0 if reason == "shutdown" else 1


async def _wait_for_shutdown_or_failure(
    stop: asyncio.Event,
    critical_tasks: list[asyncio.Task],
) -> str:
    """Return ``'shutdown'`` when the stop event fires; otherwise return the
    name of the first critical task to exit. Either outcome means the main
    loop should tear down.
    """
    stop_task = asyncio.create_task(stop.wait(), name="stop-waiter")
    try:
        done, _pending = await asyncio.wait(
            {stop_task, *critical_tasks},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        if not stop_task.done():
            stop_task.cancel()

    # Prefer reporting a dead critical task over a normal shutdown signal,
    # since the failure is the more interesting cause.
    for t in done:
        if t is stop_task:
            continue
        try:
            t.result()
            log.error("critical task %s exited cleanly (expected to run forever)", t.get_name())
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("critical task %s died", t.get_name())
        return t.get_name()

    return "shutdown"


def _export_state(config_path: str, target_path: str) -> int:
    """Copy the current state.json to ``target_path``. Loads through
    StateFile so an unreadable source bails out before clobbering the
    target.
    """
    cfg = load_config(config_path)
    src = StateFile(cfg.persistence.state_file)
    persisted = src.load()  # falls back to zeros on missing/corrupt
    dst = StateFile(target_path)
    dst.save(persisted)
    print(
        f"exported state to {target_path}: "
        f"lifetime={persisted.transfer_count_lifetime}",
        file=sys.stderr,
    )
    return 0


def _import_state(config_path: str, source_path: str) -> int:
    """Validate ``source_path`` as a PersistedState and copy it to the
    configured state file. Refuses to import if the source is unreadable
    (would otherwise wipe the live state with zeros).
    """
    import json as _json
    from pathlib import Path
    cfg = load_config(config_path)
    src_p = Path(source_path)
    if not src_p.exists():
        print(f"import-state: source file not found: {source_path}", file=sys.stderr)
        return 2
    # StateFile.load() returns zeros on corruption — that's the right
    # behaviour for normal startup, but for an explicit import we want
    # fail-fast so we don't silently zero the live state.
    with src_p.open() as f:
        try:
            _json.load(f)
        except _json.JSONDecodeError as e:
            print(
                f"import-state: refusing to import; source is not valid JSON ({e})",
                file=sys.stderr,
            )
            return 2
    src = StateFile(source_path)
    persisted = src.load()
    dst = StateFile(cfg.persistence.state_file)
    dst.save(persisted)
    print(
        f"imported state from {source_path} to {cfg.persistence.state_file}: "
        f"lifetime={persisted.transfer_count_lifetime}",
        file=sys.stderr,
    )
    print(
        "NOTE: if atspi is currently running, restart it for the new state to "
        "take effect — the in-memory snapshot is loaded once at startup.",
        file=sys.stderr,
    )
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="atspi", description="ATS-Pi companion service")
    ap.add_argument("--config", required=True, help="Path to config.yaml")
    ap.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    # State-management one-shots. Each runs and exits without starting the
    # service. Useful for: backups before deploys, cloning a unit to a
    # spare Pi, recovering after a microSD swap.
    state_grp = ap.add_mutually_exclusive_group()
    state_grp.add_argument(
        "--export-state", metavar="PATH",
        help="Copy the live state.json to PATH and exit. Validates JSON "
             "before writing.",
    )
    state_grp.add_argument(
        "--import-state", metavar="PATH",
        help="Replace state.json with the contents of PATH and exit. "
             "Stop the service first; the live process loads state once at "
             "startup.",
    )
    ap.add_argument("--version", action="version", version=f"atspi {__version__}")
    args = ap.parse_args()

    if args.export_state:
        sys.exit(_export_state(args.config, args.export_state))
    if args.import_state:
        sys.exit(_import_state(args.config, args.import_state))
    sys.exit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
