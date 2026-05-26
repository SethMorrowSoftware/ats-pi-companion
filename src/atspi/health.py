"""Optional health/status HTTP endpoint.

A localhost-bound JSON endpoint that lets external monitoring (a
cronjob, a Prometheus scraper, a dashboard, ``curl`` from the
operator's laptop) read the same state GenWatch sees over Modbus —
without speaking Modbus.

Disabled by default. Enable in config.yaml::

    health:
      enabled: true
      host: 127.0.0.1
      port: 8001

The endpoint is intentionally minimal: a single ``GET /health`` route
returning a JSON summary, 404 for anything else. No POST, no write
operations — those go through the Modbus interface.

The HTTP server runs in a daemon thread so it can read from the
RegisterStore (which holds its own lock) without coupling to the
asyncio event loop. Daemon-thread semantics mean the server dies with
the process; it has no separate lifecycle.
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from . import ICD_VERSION, __version__
from .safety import SafetyWatchdog
from .state import (
    ADDR_ATS_MODE,
    ADDR_FAULT_SUMMARY,
    ADDR_POSITION,
    ADDR_TRANSFER_COUNT_24H,
    ADDR_TRANSFER_COUNT_LIFETIME,
    ADDR_UNIT_ID,
    ADDR_UPTIME_S,
    FAULT_CALIBRATION,
    FAULT_INPUT,
    FAULT_MODE_UNKNOWN,
    FAULT_OUTPUT,
    RegisterStore,
)

log = logging.getLogger("atspi.health")

_POSITION_NAMES = {0: "utility", 1: "generator", 2: "transferring", 3: "unknown"}
_MODE_NAMES = {0: "auto", 1: "manual", 2: "test", 3: "unknown"}


def _u32_from_pair(store: RegisterStore, base_addr: int) -> int:
    hi = store.read_register(base_addr)
    lo = store.read_register(base_addr + 1)
    return (hi << 16) | lo


def _decode_fault_bits(bits: int) -> list[str]:
    """Turn the raw fault_summary register into a list of named bits.
    Stable across releases — used by external monitoring.
    """
    names = []
    if bits & FAULT_INPUT:
        names.append("input_fault")
    if bits & FAULT_OUTPUT:
        names.append("output_fault")
    if bits & FAULT_MODE_UNKNOWN:
        names.append("mode_unknown")
    if bits & FAULT_CALIBRATION:
        names.append("calibration")
    return names


def build_status(store: RegisterStore, watchdog: SafetyWatchdog) -> dict[str, Any]:
    """Snapshot the current health view. Pure function over the store —
    safe to call from any thread.
    """
    position_value = store.read_register(ADDR_POSITION)
    mode_value = store.read_register(ADDR_ATS_MODE)
    fault_bits = store.read_register(ADDR_FAULT_SUMMARY)
    last_read_age_s, released = watchdog.snapshot()
    return {
        "service": "atspi",
        "version": __version__,
        "icd_version": {"major": ICD_VERSION[0], "minor": ICD_VERSION[1]},
        "unit_id": store.read_register(ADDR_UNIT_ID),
        "uptime_s": _u32_from_pair(store, ADDR_UPTIME_S),
        "position": _POSITION_NAMES.get(position_value, f"unknown({position_value})"),
        "ats_mode": _MODE_NAMES.get(mode_value, f"unknown({mode_value})"),
        "fault_summary": {
            "raw": fault_bits,
            "active": _decode_fault_bits(fault_bits),
        },
        "transfer_count_lifetime": _u32_from_pair(store, ADDR_TRANSFER_COUNT_LIFETIME),
        "transfer_count_24h": _u32_from_pair(store, ADDR_TRANSFER_COUNT_24H),
        "last_modbus_read_age_s": round(last_read_age_s, 2),
        "watchdog_released": released,
    }


def _make_handler(store: RegisterStore, watchdog: SafetyWatchdog):
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler interface)
            if self.path not in ("/health", "/health/"):
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"not found\n")
                return
            try:
                body = build_status(store, watchdog)
            except Exception as e:  # noqa: BLE001
                self.send_response(500)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(f"health probe failed: {e}\n".encode())
                return
            payload = json.dumps(body, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            # Route the access log into our logger at DEBUG level instead
            # of stderr, so it doesn't drown out the service log.
            log.debug("%s - %s", self.address_string(), format % args)

    return HealthHandler


class HealthServer:
    """Wraps the thread + HTTPServer so __main__ can start and stop it
    deterministically.
    """

    def __init__(self, host: str, port: int, store: RegisterStore, watchdog: SafetyWatchdog):
        self._httpd = HTTPServer((host, port), _make_handler(store, watchdog))
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="atspi-health",
            daemon=True,
        )

    def start(self) -> None:
        log.info(
            "health endpoint listening on http://%s:%d/health",
            *self._httpd.server_address[:2],
        )
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        # join with a short timeout — the daemon thread will be torn
        # down with the process anyway, but a clean join keeps tests
        # deterministic.
        self._thread.join(timeout=2.0)


def start_health_server(
    host: str, port: int, store: RegisterStore, watchdog: SafetyWatchdog,
) -> HealthServer:
    server = HealthServer(host, port, store, watchdog)
    server.start()
    return server
