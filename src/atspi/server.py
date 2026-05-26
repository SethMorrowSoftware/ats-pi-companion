"""Modbus TCP server (pymodbus). Mounts a RegisterStore behind the
standard Modbus address space and serves reads/writes per the ICD.

The data block subclass routes ``getValues`` / ``setValues`` calls
into the RegisterStore. Recognized writes are translated into
:class:`CommandIntent` objects and dispatched to the I/O driver via
the ``on_command`` callback supplied by the caller. The server itself
holds no state.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartAsyncTcpServer

from .state import CommandIntent, RegisterStore

log = logging.getLogger("atspi.server")

# Cap on how long start_server() waits for the listening socket to come up.
# pymodbus binds within a few ms locally; 5 s is generous headroom for slow CI.
_BIND_TIMEOUT_S = 5.0
_BIND_POLL_INTERVAL_S = 0.02


def _make_data_block(
    store: RegisterStore,
    on_read: Callable[[], None] | None,
    on_command: Callable[[CommandIntent], None] | None,
):
    """Build a pymodbus data block that proxies all access to the
    RegisterStore. The on_read callback fires after every read (used
    by the safety watchdog). The on_command callback fires when a
    recognized command write arrives.
    """

    class LiveDataBlock(ModbusSequentialDataBlock):
        def getValues(self, address, count=1):  # noqa: N802 (pymodbus interface)
            # pymodbus passes 1-based addresses
            if on_read is not None:
                on_read()
            return [store.read_register(address - 1 + i) for i in range(count)]

        def setValues(self, address, values):  # noqa: N802
            for i, v in enumerate(values):
                intent = store.write_register(address - 1 + i, int(v))
                if intent is not None and on_command is not None:
                    on_command(intent)

    # Allocate enough address space for the ICD's register layout
    # (0x0000-0x010F + spare). Values are unused — overridden by
    # getValues/setValues.
    return LiveDataBlock(0, [0] * 0x0200)


async def start_server(
    host: str,
    port: int,
    unit_id: int,
    store: RegisterStore,
    on_read: Callable[[], None] | None = None,
    on_command: Callable[[CommandIntent], None] | None = None,
) -> asyncio.Task:
    """Start the Modbus TCP server as a background task. Returns the
    task handle so the caller can cancel it during shutdown.
    """
    block = _make_data_block(store, on_read, on_command)
    slave = ModbusSlaveContext(hr=block, ir=block)
    context = ModbusServerContext(slaves={unit_id: slave}, single=False)

    async def _serve():
        try:
            await StartAsyncTcpServer(context=context, address=(host, port))
        except asyncio.CancelledError:
            log.info("Modbus server cancelled")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("Modbus server crashed: %s", e)
            raise

    log.info("Modbus TCP server starting on %s:%d (unit_id=%d)", host, port, unit_id)
    task = asyncio.create_task(_serve(), name="modbus-server")
    await _wait_until_bound(host, port, task)
    return task


async def _wait_until_bound(host: str, port: int, server_task: asyncio.Task) -> None:
    """Block until the listening socket accepts a TCP connection.

    Replaces a fixed asyncio.sleep(0.1) which was racy under load and on
    slow CI runners — sometimes start_server returned while pymodbus was
    still mid-bind and the first client got connection-refused.
    """
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    deadline = time.monotonic() + _BIND_TIMEOUT_S
    last_err: BaseException | None = None
    while time.monotonic() < deadline:
        if server_task.done():
            # Server died before binding — surface the underlying error.
            server_task.result()
            raise RuntimeError("Modbus server task exited before binding")
        try:
            _r, w = await asyncio.open_connection(probe_host, port)
        except (ConnectionRefusedError, OSError) as e:
            last_err = e
            await asyncio.sleep(_BIND_POLL_INTERVAL_S)
            continue
        w.close()
        try:
            await w.wait_closed()
        except (ConnectionError, OSError):
            pass
        return
    raise TimeoutError(
        f"Modbus server failed to bind {host}:{port} within "
        f"{_BIND_TIMEOUT_S:.1f}s: {last_err}"
    )
