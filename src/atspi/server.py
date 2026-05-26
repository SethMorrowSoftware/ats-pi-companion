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
from collections.abc import Callable

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartAsyncTcpServer

from .state import (
    ADDR_CMD_BYPASS_DELAY,
    ADDR_CMD_FORCE_TRANSFER,
    ADDR_CMD_INHIBIT,
    ADDR_CMD_TEST,
    CommandIntent,
    RegisterStore,
)

log = logging.getLogger("atspi.server")


# Per ICD §5, writes are only allowed to the four command registers in the
# holding-register space. Every other address — read-only state, reserved
# holes, the coil/discrete spaces — must reject writes with a Modbus exception
# so the client knows the command did not take effect.
_ALLOWED_HOLDING_WRITE_ADDRESSES = frozenset([
    ADDR_CMD_TEST,
    ADDR_CMD_INHIBIT,
    ADDR_CMD_FORCE_TRANSFER,
    ADDR_CMD_BYPASS_DELAY,
])

# FC06 (write single register), FC16 (write multiple registers).
_HOLDING_WRITE_FCS = frozenset([0x06, 0x10])
# FC05 (write single coil), FC15 (write multiple coils).
_COIL_WRITE_FCS = frozenset([0x05, 0x0F])


class _GuardedSlaveContext(ModbusSlaveContext):
    """Slave context that refuses writes outside the four ICD command
    registers. Returns Modbus exception 0x02 (illegal data address); the ICD
    specifies 0x03 (illegal data value) for reserved-write rejection, but
    pymodbus's validate() path triggers 0x02 — practically equivalent since
    both signal "write rejected" to the client.

    The ATS-Pi only exposes holding registers, so coil-write FCs (FC05/FC15)
    are rejected unconditionally.
    """

    def validate(self, fc_as_hex, address, count=1):  # noqa: N803 (pymodbus interface)
        if fc_as_hex in _COIL_WRITE_FCS:
            return False
        if fc_as_hex in _HOLDING_WRITE_FCS:
            for offset in range(count):
                if (address + offset) not in _ALLOWED_HOLDING_WRITE_ADDRESSES:
                    return False
        return super().validate(fc_as_hex, address, count)


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
    slave = _GuardedSlaveContext(hr=block, ir=block)
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
    # Give the server a moment to bind
    await asyncio.sleep(0.1)
    return task
