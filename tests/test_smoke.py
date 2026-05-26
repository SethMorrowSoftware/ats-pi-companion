"""Smoke tests — sanity checks that everything imports and basic plumbing works."""
from __future__ import annotations

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
    assert cfg.modbus_server.port == 502
    assert cfg.io.driver == "mock"


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
