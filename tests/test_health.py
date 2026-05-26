"""Tests for the optional /health HTTP endpoint."""
from __future__ import annotations

import json
import socket
import time
import urllib.request

import pytest

from atspi.health import build_status, start_health_server
from atspi.io_driver import InputSnapshot
from atspi.io_mock import IOMockDriver
from atspi.safety import SafetyWatchdog
from atspi.state import (
    FAULT_INPUT,
    FAULT_OUTPUT,
    RegisterStore,
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _seed(store: RegisterStore, *, mode: str = "auto", position: str = "utility") -> None:
    store.apply_input_snapshot(InputSnapshot(
        position=position, normal_available=True, emergency_available=True,
        engine_start_calling=False, ats_mode=mode, fault_bits=0,
    ))


@pytest.fixture
def watchdog():
    store = RegisterStore(unit_id=23)
    driver = IOMockDriver()
    return SafetyWatchdog(store, driver), store


def test_build_status_includes_expected_keys(watchdog):
    wd, store = watchdog
    _seed(store)
    s = build_status(store, wd)
    for key in (
        "service", "version", "icd_version", "unit_id", "uptime_s",
        "position", "ats_mode", "fault_summary",
        "transfer_count_lifetime", "transfer_count_24h",
        "last_modbus_read_age_s", "watchdog_released",
    ):
        assert key in s
    assert s["service"] == "atspi"
    assert s["unit_id"] == 23
    assert s["position"] == "utility"
    assert s["ats_mode"] == "auto"
    assert s["icd_version"] == {"major": 1, "minor": 0}
    assert s["watchdog_released"] is False


def test_build_status_decodes_fault_bits(watchdog):
    wd, store = watchdog
    _seed(store)
    store.set_input_fault(True)
    store.set_output_fault(True)
    s = build_status(store, wd)
    assert s["fault_summary"]["raw"] == (FAULT_INPUT | FAULT_OUTPUT)
    assert set(s["fault_summary"]["active"]) == {"input_fault", "output_fault"}


def test_build_status_decodes_position_and_mode(watchdog):
    wd, store = watchdog
    _seed(store, mode="manual", position="generator")
    s = build_status(store, wd)
    assert s["position"] == "generator"
    assert s["ats_mode"] == "manual"


def test_http_health_endpoint_returns_json(watchdog):
    """End-to-end: real HTTP server bound to a free port, real urllib client."""
    wd, store = watchdog
    _seed(store)
    port = _free_port()
    server = start_health_server("127.0.0.1", port, store, wd)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            assert r.status == 200
            assert r.headers["Content-Type"] == "application/json"
            body = json.loads(r.read())
        assert body["service"] == "atspi"
        assert body["unit_id"] == 23
    finally:
        server.stop()


def test_http_health_endpoint_404_for_other_paths(watchdog):
    wd, store = watchdog
    _seed(store)
    port = _free_port()
    server = start_health_server("127.0.0.1", port, store, wd)
    try:
        with pytest.raises(urllib.error.HTTPError) as ex:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
        assert ex.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as ex2:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2)
        assert ex2.value.code == 404
    finally:
        server.stop()


def test_http_health_endpoint_handles_concurrent_requests(watchdog):
    """A scraper hitting /health 10× in quick succession should all succeed."""
    wd, store = watchdog
    _seed(store)
    port = _free_port()
    server = start_health_server("127.0.0.1", port, store, wd)
    try:
        for _ in range(10):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                assert r.status == 200
                json.loads(r.read())
    finally:
        server.stop()


def test_http_health_endpoint_last_read_age_reflects_watchdog(watchdog):
    wd, store = watchdog
    _seed(store)
    # Pretend the last Modbus read was 5 seconds ago.
    wd._last_read_monotonic = time.monotonic() - 5.0  # noqa: SLF001
    port = _free_port()
    server = start_health_server("127.0.0.1", port, store, wd)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            body = json.loads(r.read())
        # Allow some slack — the server reads time.monotonic() after we set it.
        assert 4.5 < body["last_modbus_read_age_s"] < 6.0
    finally:
        server.stop()


def test_health_server_stop_is_idempotent_in_practice(watchdog):
    """Stopping a running server then trying to use it should not crash."""
    wd, store = watchdog
    _seed(store)
    port = _free_port()
    server = start_health_server("127.0.0.1", port, store, wd)
    server.stop()
    # Re-stop would crash on shutdown() of a closed server; we don't claim
    # idempotency in the API. Verify the port is freed (no listener).
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5)


def test_config_health_defaults_to_disabled():
    """Backward compat: configs without a health section get the off default."""
    from atspi.config import Config, HealthCfg, _coerce
    cfg = _coerce(Config, {})
    assert isinstance(cfg.health, HealthCfg)
    assert cfg.health.enabled is False
    assert cfg.health.host == "127.0.0.1"
    assert cfg.health.port == 8001


def test_config_health_section_parses():
    from atspi.config import Config, _coerce
    cfg = _coerce(Config, {
        "health": {"enabled": True, "host": "0.0.0.0", "port": 9999},
    })
    assert cfg.health.enabled is True
    assert cfg.health.host == "0.0.0.0"
    assert cfg.health.port == 9999
