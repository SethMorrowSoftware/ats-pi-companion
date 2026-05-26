"""Smoke tests — sanity checks that everything imports and basic plumbing works."""
from __future__ import annotations

import asyncio

import pytest

import atspi
from atspi.config import Config, load_config
from atspi.io_driver import InputSnapshot, OutputState
from atspi.io_mock import IOMockDriver
from atspi.state import RegisterStore


def test_version_present():
    assert atspi.__version__
    assert atspi.ICD_VERSION == (1, 0)


def test_default_config_loads(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("modbus_server:\n  port: 5020\nsite:\n  unit_id: 99\n")
    cfg = load_config(p)
    assert isinstance(cfg, Config)
    assert cfg.modbus_server.port == 5020
    assert cfg.site.unit_id == 99


@pytest.mark.asyncio
async def test_mock_driver_round_trip():
    driver = IOMockDriver()
    await driver.connect()
    snap = await driver.read_inputs()
    assert isinstance(snap, InputSnapshot)
    assert snap.normal_available is True
    assert snap.position == "utility"

    # Drive a maintained output, read back
    await driver.drive_outputs(inhibit=True)
    out = await driver.read_output_state()
    assert isinstance(out, OutputState)
    assert out.inhibit_active is True
    await driver.close()


def test_register_store_publishes_default_state():
    store = RegisterStore(unit_id=23)
    # Position = 'unknown' (3) until first sampling cycle
    assert store.read_register(0x0000) == 3
    # ICD version
    assert store.read_register(0x0030) == 1
    assert store.read_register(0x0031) == 0
    # Unit ID
    assert store.read_register(0x0035) == 23


def test_register_store_reserved_addresses_read_zero():
    store = RegisterStore()
    # ICD §5: reserved addresses MUST return 0
    assert store.read_register(0x0050) == 0
    assert store.read_register(0x00FF) == 0
    assert store.read_register(0x0200) == 0


@pytest.mark.asyncio
async def test_mock_pulse_re_trigger_during_active_is_ignored():
    """ICD §6: mock driver must also enforce pulse idempotency."""
    d = IOMockDriver()
    await d.connect()
    await d.drive_outputs(test_pulse_ms=1500)
    out_first = await d.read_output_state()
    assert out_first.test_active is True
    # Re-trigger mid-pulse — must not extend or restart.
    await d.drive_outputs(test_pulse_ms=1500)
    # Wait less than the original pulse but past where a re-trigger would push it.
    await asyncio.sleep(0.6)
    # Pulse is still in its original window.
    out_mid = await d.read_output_state()
    assert out_mid.test_active is True
    # After the original pulse window completes, it must self-clear.
    await asyncio.sleep(1.2)
    out_after = await d.read_output_state()
    assert out_after.test_active is False, (
        "Original pulse must release on its original schedule, "
        "not be extended by the second drive_outputs call"
    )
    await d.close()
