"""C-1 regression: the comms-loss watchdog is scoped to GenWatch's connection.

The naive watchdog re-armed on *any* successful Modbus read, so a diagnostic
poller / scanner / stale second GenWatch on a separate connection could keep a
latched force-transfer/inhibit asserted forever — defeating the central §8.3
safety rule. These tests drive a real Modbus TCP server with two real client
connections and prove:

  1. a busy diagnostic reader CANNOT keep a latched command alive;
  2. the commanding connection's own reads DO keep it alive (no false release);
  3. dropping the commanding connection releases immediately (ICD §9.1).
"""
from __future__ import annotations

import asyncio

from pymodbus.client import AsyncModbusTcpClient

from atspi.io_driver import InputSnapshot
from atspi.io_mock import IOMockDriver
from atspi.safety import SafetyWatchdog
from atspi.server import start_server
from atspi.state import ADDR_CMD_INHIBIT, ADDR_CMD_INHIBIT_RB, RegisterStore


async def _setup(port, monkeypatch, *, timeout_s=0.3):
    """Start a real server + watchdog wired to a mock driver. Returns the
    pieces plus a cleanup coroutine.
    """
    import atspi.safety as safety_mod
    monkeypatch.setattr(safety_mod, "TIMEOUT_S", timeout_s)
    monkeypatch.setattr(safety_mod, "CHECK_INTERVAL_S", 0.05)

    store = RegisterStore()
    # Seed ats_mode=auto so the mode gate (ICD §6) permits the inhibit command.
    store.apply_input_snapshot(InputSnapshot(
        position="utility", normal_available=True, emergency_available=True,
        engine_start_calling=False, ats_mode="auto", fault_bits=0,
    ))
    driver = IOMockDriver()
    await driver.connect()
    loop = asyncio.get_running_loop()

    async def dispatch(intent):
        await driver.drive_outputs(
            test_pulse_ms=intent.test_pulse_ms,
            inhibit=intent.inhibit,
            force_transfer=intent.force_transfer,
            bypass_delay_pulse_ms=intent.bypass_delay_pulse_ms,
        )
        store.apply_output_state(await driver.read_output_state())

    def on_command(intent):
        loop.create_task(dispatch(intent))

    watchdog = SafetyWatchdog(store, driver)
    wd_task = asyncio.create_task(watchdog.run(), name="wd")
    server_task = await start_server(
        host="127.0.0.1", port=port, unit_id=1, store=store,
        on_command=on_command, watchdog=watchdog,
    )

    async def cleanup():
        wd_task.cancel()
        server_task.cancel()
        for t in (wd_task, server_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await driver.close()

    return store, driver, watchdog, cleanup


async def test_diagnostic_reader_cannot_keep_latched_command_alive(
    unused_tcp_port, monkeypatch,
):
    store, _driver, watchdog, cleanup = await _setup(unused_tcp_port, monkeypatch)
    commander = AsyncModbusTcpClient(host="127.0.0.1", port=unused_tcp_port)
    diag = AsyncModbusTcpClient(host="127.0.0.1", port=unused_tcp_port)
    await commander.connect()
    await diag.connect()
    try:
        # Commander (GenWatch) asserts inhibit, then falls silent.
        r = await commander.write_register(address=ADDR_CMD_INHIBIT, value=1, slave=1)
        assert not r.isError()
        await asyncio.sleep(0.1)
        assert store.read_register(ADDR_CMD_INHIBIT_RB) == 1

        # A diagnostic tool hammers reads for well over the timeout window.
        for _ in range(20):
            rr = await diag.read_holding_registers(address=0x0000, count=6, slave=1)
            assert not rr.isError()
            await asyncio.sleep(0.05)

        # The diagnostic reads must NOT have kept the command alive.
        assert store.read_register(ADDR_CMD_INHIBIT_RB) == 0
        assert watchdog._released is True  # noqa: SLF001
    finally:
        commander.close()
        diag.close()
        await cleanup()


async def test_commanders_own_reads_keep_command_alive(unused_tcp_port, monkeypatch):
    store, _driver, watchdog, cleanup = await _setup(unused_tcp_port, monkeypatch)
    commander = AsyncModbusTcpClient(host="127.0.0.1", port=unused_tcp_port)
    await commander.connect()
    try:
        r = await commander.write_register(address=ADDR_CMD_INHIBIT, value=1, slave=1)
        assert not r.isError()

        # The commander keeps polling for longer than the timeout — its OWN
        # reads must re-arm the watchdog, so the command stays asserted.
        for _ in range(20):
            rr = await commander.read_holding_registers(address=0x0000, count=6, slave=1)
            assert not rr.isError()
            await asyncio.sleep(0.05)

        assert store.read_register(ADDR_CMD_INHIBIT_RB) == 1
        assert watchdog._released is False  # noqa: SLF001
    finally:
        commander.close()
        await cleanup()


async def test_commander_drop_releases_immediately(unused_tcp_port, monkeypatch):
    # Long silence timeout so a release within a few ticks proves it was the
    # connection DROP that triggered it, not the 30 s window.
    store, _driver, watchdog, cleanup = await _setup(
        unused_tcp_port, monkeypatch, timeout_s=5.0,
    )
    commander = AsyncModbusTcpClient(host="127.0.0.1", port=unused_tcp_port)
    await commander.connect()
    try:
        r = await commander.write_register(address=ADDR_CMD_INHIBIT, value=1, slave=1)
        assert not r.isError()
        await asyncio.sleep(0.1)
        assert store.read_register(ADDR_CMD_INHIBIT_RB) == 1

        # Commander drops its TCP connection.
        commander.close()

        # Released well within the 5 s silence window (drop-triggered).
        await asyncio.sleep(0.4)
        assert store.read_register(ADDR_CMD_INHIBIT_RB) == 0
        assert watchdog._released is True  # noqa: SLF001
    finally:
        commander.close()
        await cleanup()
