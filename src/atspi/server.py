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
import contextlib
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartAsyncTcpServer
from pymodbus.server.async_io import ModbusServerRequestHandler, ModbusTcpServer

from .state import (
    ADDR_CMD_BYPASS_DELAY,
    ADDR_CMD_FORCE_TRANSFER,
    ADDR_CMD_INHIBIT,
    ADDR_CMD_TEST,
    CommandIntent,
    RegisterStore,
)

if TYPE_CHECKING:
    from .safety import SafetyWatchdog

log = logging.getLogger("atspi.server")

# Cap on how long start_server() waits for the listening socket to come up.
# pymodbus binds within a few ms locally; 5 s is generous headroom for slow CI.
_BIND_TIMEOUT_S = 5.0
_BIND_POLL_INTERVAL_S = 0.02


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
# FC23 (read/write multiple registers). It writes, but pymodbus validates its
# read-range and write-range under this same function code, so validate()
# cannot gate the write-range independently of the read-range. The ATS-Pi does
# not define FC23 (clients read via FC03/FC04 and write via FC06/FC16 per the
# ICD), so reject it wholesale — otherwise an FC23 write to a read-only or
# reserved address falls through to the bounds-only default validate() and is
# wrongly accepted, violating the ICD §6.1 reject-undefined-writes contract.
_UNSUPPORTED_WRITE_FCS = frozenset([0x17])
# FC03 (read holding regs), FC04 (read input regs); both serve the same
# ICD register space.
_HOLDING_READ_FCS = frozenset([0x03, 0x04])


class _GuardedSlaveContext(ModbusSlaveContext):
    """Slave context that refuses writes the ICD says must be rejected.

    Four classes of rejection:

      * Coil writes (FC05/FC15) — the ATS-Pi exposes no coils; reject
        unconditionally regardless of address.
      * FC23 (read/write multiple registers) — a write-capable function
        code the ATS-Pi does not define; reject unconditionally (see
        ``_UNSUPPORTED_WRITE_FCS`` for why the write-range can't be gated
        on its own).
      * Holding writes (FC06/FC16) outside the four ICD command
        registers (``0x0100``–``0x0103``) — reserved space, read-only
        identification, and timestamp registers all live outside that
        band; the ICD requires writes there be rejected.
      * Holding writes inside the command band but not permitted by the
        current ``ats_mode`` — ICD §6 mode policy. ``RegisterStore``
        also latches ``mode_reject_active`` so ``fault_summary`` surfaces
        the rejection on the next read.

    All three return Modbus exception 0x02 (illegal data address). The
    ICD prefers 0x03 (illegal data value) for reserved-range rejection
    and 0x04 (server device failure) for mode rejection, but pymodbus's
    ``validate()`` hook only emits 0x02. The safety-relevant property
    (write rejected with a Modbus exception, client knows) holds; the
    exact code is documented as a known deviation in CHANGELOG.

    Mode enforcement lives here rather than in the data block's
    ``setValues`` because pymodbus only translates ``validate()=False``
    into an exception response — raising from ``setValues`` would not
    yield a clean Modbus error for the client.

    Value-level validation (ICD §6: out-of-pattern values must get 0x03)
    cannot live here either — ``validate()`` never sees the written value.
    Such writes are acknowledged and then dropped by
    ``RegisterStore.write_register`` (no intent → no relay action); known
    deviation, pinned by the ICD contract suite. See CHANGELOG.
    """

    def __init__(self, *args, store: RegisterStore, **kwargs):
        super().__init__(*args, **kwargs)
        self._store = store

    def validate(self, fc_as_hex, address, count=1):  # noqa: N803 (pymodbus interface)
        if fc_as_hex in _COIL_WRITE_FCS or fc_as_hex in _UNSUPPORTED_WRITE_FCS:
            return False
        if fc_as_hex in _HOLDING_WRITE_FCS:
            for offset in range(count):
                target = address + offset
                if target not in _ALLOWED_HOLDING_WRITE_ADDRESSES:
                    return False
                # Mode policy: latches mode_reject_active on rejection.
                if not self._store.can_write(target):
                    return False
            return True
        if fc_as_hex in _HOLDING_READ_FCS:
            # ICD §3: ALL reserved holding-register addresses through
            # 0xFFFF MUST return 0 on read. The default
            # SequentialDataBlock.validate rejects addresses past the
            # block's allocated size with exception 0x02; that breaks
            # GenWatch's reserved-range probes. Accept any read address
            # — getValues delegates to RegisterStore.read_register which
            # already returns 0 for unknown addresses.
            return True
        # For function codes we don't explicitly handle (coil reads,
        # diagnostics), fall back to the default behaviour rather than
        # silently accepting.
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
            # Pin time once for the whole multi-word read so u32 fields
            # (uptime_s at 0x0014, wallclock at 0x0016) return a coherent
            # high/low pair even if this call straddles a second boundary.
            now_mono = time.monotonic()
            now_wall = int(time.time())
            return [
                store.read_register(
                    address - 1 + i, now_mono=now_mono, now_wall=now_wall,
                )
                for i in range(count)
            ]

        def setValues(self, address, values):  # noqa: N802
            for i, v in enumerate(values):
                intent = store.write_register(address - 1 + i, int(v))
                if intent is not None and on_command is not None:
                    on_command(intent)

    # Allocate enough address space for the ICD's register layout
    # (0x0000-0x010F + spare). Values are unused — overridden by
    # getValues/setValues.
    return LiveDataBlock(0, [0] * 0x0200)


class _WatchdogProto(Protocol):
    """The slice of SafetyWatchdog the connection tracker drives."""

    def note_modbus_read(self) -> None: ...
    def note_commander_lost(self) -> None: ...


# The four command registers (0x0100–0x0103). A holding write into this band is
# how GenWatch drives the switch — diagnostic tools only read — so it is the
# signal we use to identify the authoritative connection.
_CMD_ADDR_LO = ADDR_CMD_TEST
_CMD_ADDR_HI = ADDR_CMD_BYPASS_DELAY


class ConnectionTracker:
    """Scopes the comms-loss watchdog to GenWatch's connection (ICD §3/§8.3).

    The ICD names GenWatch's single persistent connection as the authoritative
    one; the ATS-Pi may accept other connections (diagnostic tools) but must
    not treat them as GenWatch. The naive watchdog re-armed on *any* read, so a
    `modpoll` loop, a scanner, or a stale second GenWatch could indefinitely
    suppress the auto-release of a latched force-transfer/inhibit — defeating
    the central safety rule.

    We identify the authoritative connection as **the one that issues command
    writes**: only GenWatch commands the switch (diagnostic tools read). From
    then on, only that connection's activity re-arms the watchdog; reads from
    any other connection are ignored. A drop of the commanding connection
    triggers an immediate release.

    Before any command has ever been issued there is nothing to protect, so any
    activity re-arms (prevents a spurious release at startup).

    Connection identity is the per-connection request-handler instance (one per
    TCP connection in pymodbus); it is used only as an opaque key.
    """

    _HOLDING_WRITE_FCS = frozenset([0x06, 0x10])

    def __init__(self, watchdog: _WatchdogProto):
        self._watchdog = watchdog
        self._commander: object | None = None

    def note_request(self, conn: object, function_code: int | None,
                     address: int | None) -> None:
        is_cmd_write = (
            function_code in self._HOLDING_WRITE_FCS
            and address is not None
            and _CMD_ADDR_LO <= address <= _CMD_ADDR_HI
        )
        if is_cmd_write:
            # Whoever commands the switch is, by definition, authoritative.
            if conn is not self._commander:
                self._commander = conn
                log.info("authoritative (commanding) connection established")
            self._watchdog.note_modbus_read()
        elif self._commander is None:
            # No command issued yet → nothing to protect; stay armed off any
            # activity so the watchdog can't fire before GenWatch asserts.
            self._watchdog.note_modbus_read()
        elif conn is self._commander:
            self._watchdog.note_modbus_read()
        # else: a read from a non-commanding connection — ignored (the fix).

    def note_disconnect(self, conn: object) -> None:
        if conn is self._commander:
            self._commander = None
            log.warning("commanding connection dropped — releasing per ICD §8.3")
            self._watchdog.note_commander_lost()


def _make_tracking_handler(tracker: ConnectionTracker):
    """Build a per-connection request handler that feeds the tracker.

    pymodbus instantiates one handler per TCP connection, so the handler
    instance *is* the connection identity. ``_async_execute`` runs for every
    decoded request (it has both the connection — ``self`` — and the request's
    function code/address), and ``callback_disconnected`` fires on close.
    """

    class _TrackingRequestHandler(ModbusServerRequestHandler):
        async def _async_execute(self, request, *addr):  # noqa: ANN001
            try:
                tracker.note_request(
                    self,
                    getattr(request, "function_code", None),
                    getattr(request, "address", None),
                )
            except Exception:  # never let tracking break request handling
                log.exception("connection tracker note_request failed")
            return await super()._async_execute(request, *addr)

        def callback_disconnected(self, exc) -> None:  # noqa: ANN001
            try:
                tracker.note_disconnect(self)
            except Exception:  # never let tracking break teardown
                log.exception("connection tracker note_disconnect failed")
            super().callback_disconnected(exc)

    return _TrackingRequestHandler


def _make_tracking_server(context, address, tracker: ConnectionTracker):
    """ModbusTcpServer that hands out tracking request handlers.

    pymodbus exposes no public hook to supply a custom handler, but the server
    creates each one via ``callback_new_connection`` — overriding that is the
    supported-enough injection point (pinned to pymodbus 3.7.x).
    """
    handler_cls = _make_tracking_handler(tracker)

    class _TrackingTcpServer(ModbusTcpServer):
        def callback_new_connection(self):
            return handler_cls(self)

    # Default framer for ModbusTcpServer is FramerType.SOCKET (MBAP) — the ICD
    # transport. address is (host, port).
    return _TrackingTcpServer(context, address=address)


async def start_server(
    host: str,
    port: int,
    unit_id: int,
    store: RegisterStore,
    on_read: Callable[[], None] | None = None,
    on_command: Callable[[CommandIntent], None] | None = None,
    watchdog: SafetyWatchdog | None = None,
) -> asyncio.Task:
    """Start the Modbus TCP server as a background task. Returns the
    task handle so the caller can cancel it during shutdown.

    When ``watchdog`` is supplied, the server scopes the comms-loss watchdog to
    GenWatch's (the commanding) connection via :class:`ConnectionTracker`, so a
    diagnostic reader on a second connection cannot keep maintained commands
    alive (ICD §8.3). ``on_read`` is the legacy data-block-level hook used by
    tests; production passes ``watchdog`` instead.
    """
    block = _make_data_block(store, on_read, on_command)
    slave = _GuardedSlaveContext(hr=block, ir=block, store=store)
    context = ModbusServerContext(slaves={unit_id: slave}, single=False)

    if watchdog is not None:
        tracker = ConnectionTracker(watchdog)
        server = _make_tracking_server(context, (host, port), tracker)

        async def _serve():
            try:
                # Mirrors StartAsyncTcpServer/_serverList.run (serve_forever
                # with CancelledError suppressed) but with our handler class.
                with contextlib.suppress(asyncio.CancelledError):
                    await server.serve_forever()
            except Exception as e:  # noqa: BLE001
                log.exception("Modbus server crashed: %s", e)
                raise
    else:
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
