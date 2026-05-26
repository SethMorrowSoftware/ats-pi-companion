"""YAML config loader. Pydantic-light — uses dataclasses + manual
validation since pulling in a heavy schema lib for this small a config
isn't worth it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised on structural config problems (unknown keys, bad driver name).

    Fail-fast on a typo'd config key is the whole point — silently dropping
    keys is the production hazard we're protecting against.
    """


@dataclass
class ModbusServerCfg:
    host: str = "0.0.0.0"
    port: int = 502
    unit_id: int = 1


@dataclass
class AdamCfg:
    host: str = "192.168.1.251"
    port: int = 502
    unit_id: int = 1


@dataclass
class IOCfg:
    driver: str = "mock"  # 'mock' | 'adam'
    adam: AdamCfg = field(default_factory=AdamCfg)


@dataclass
class SiteCfg:
    # Reported via the ats_pi_unit_id register (ICD §5.4). GenWatch uses
    # this for the expected-unit-id sanity check.
    unit_id: int = 1


@dataclass
class PersistenceCfg:
    state_file: str = "/var/lib/atspi/state.json"


@dataclass
class Config:
    modbus_server: ModbusServerCfg = field(default_factory=ModbusServerCfg)
    io: IOCfg = field(default_factory=IOCfg)
    site: SiteCfg = field(default_factory=SiteCfg)
    persistence: PersistenceCfg = field(default_factory=PersistenceCfg)


def _coerce(cls, data: dict[str, Any], _path: str = ""):
    """Dict → dataclass with strict unknown-key checking.

    A typo'd key in production silently fed defaults to the running service —
    by the time anyone noticed, the wrong port / unit_id / driver had been
    used for hours. Raise on unknowns instead.
    """
    out = cls()
    for k, v in (data or {}).items():
        if not hasattr(out, k):
            known = sorted(out.__dataclass_fields__.keys())
            location = f"{_path}.{k}" if _path else k
            raise ConfigError(
                f"unknown config key {location!r}; valid keys at this level: {known}"
            )
        attr = getattr(out, k)
        if hasattr(attr, "__dataclass_fields__") and isinstance(v, dict):
            child_path = f"{_path}.{k}" if _path else k
            setattr(out, k, _coerce(type(attr), v, _path=child_path))
        else:
            setattr(out, k, v)
    return out


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config not found: {p}")
    with p.open() as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping, got {type(raw).__name__}")
    return _coerce(Config, raw)
