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


def test_config_rejects_typo_in_top_level_key(tmp_path):
    """An ops typo at the top level used to be silently ignored. Now fails fast."""
    from atspi.config import ConfigError
    p = tmp_path / "cfg.yaml"
    p.write_text("modbussserver:\n  port: 5020\n")  # double-s typo
    with pytest.raises(ConfigError, match="modbussserver"):
        load_config(p)


def test_config_rejects_typo_in_nested_key(tmp_path):
    """Nested-key typos also fail fast, with the dotted path in the message."""
    from atspi.config import ConfigError
    p = tmp_path / "cfg.yaml"
    p.write_text("io:\n  drivr: mock\n")  # 'drivr' instead of 'driver'
    with pytest.raises(ConfigError, match="io.drivr"):
        load_config(p)


def test_config_rejects_non_mapping_root(tmp_path):
    from atspi.config import ConfigError
    p = tmp_path / "cfg.yaml"
    p.write_text("- a\n- b\n")  # YAML list, not a mapping
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_config(p)


def test_config_accepts_empty_file(tmp_path):
    """An empty YAML file loads as all-defaults."""
    p = tmp_path / "cfg.yaml"
    p.write_text("")
    cfg = load_config(p)
    assert cfg.modbus_server.port == 5020
    assert cfg.io.driver == "mock"


def test_adam_config_defaults_and_overrides(tmp_path):
    """The ADAM driver knobs (debounce, assumed_mode) load with sane defaults
    and accept overrides through the strict loader.
    """
    p = tmp_path / "cfg.yaml"
    p.write_text("io:\n  driver: adam\n")
    cfg = load_config(p)
    assert cfg.io.adam.debounce_samples == 3
    assert cfg.io.adam.assumed_mode == "auto"

    p.write_text(
        "io:\n"
        "  driver: adam\n"
        "  adam:\n"
        "    host: 10.0.0.9\n"
        "    debounce_samples: 5\n"
        "    assumed_mode: manual\n"
    )
    cfg = load_config(p)
    assert cfg.io.adam.host == "10.0.0.9"
    assert cfg.io.adam.debounce_samples == 5
    assert cfg.io.adam.assumed_mode == "manual"


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


async def test_mock_release_all_outputs_clears_everything():
    """release_all_outputs (ICD §9.3 reset / shutdown cleanup) drops the
    maintained pair AND an in-flight pulse on the mock driver."""
    d = IOMockDriver()
    await d.connect()
    await d.drive_outputs(inhibit=True, force_transfer=True, test_pulse_ms=1500)
    await d.release_all_outputs()
    out = await d.read_output_state()
    assert out.test_active is False
    assert out.inhibit_active is False
    assert out.force_transfer_active is False
    assert out.bypass_delay_active is False
    await d.close()
