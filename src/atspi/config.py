"""YAML config loader. Pydantic-light — uses dataclasses + manual
validation since pulling in a heavy schema lib for this small a config
isn't worth it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


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
class HealthCfg:
    # Localhost-bound JSON status endpoint. Off by default so the default
    # production install has no extra listening port; opt-in for sites
    # that want external monitoring without speaking Modbus.
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8001


@dataclass
class Config:
    modbus_server: ModbusServerCfg = field(default_factory=ModbusServerCfg)
    io: IOCfg = field(default_factory=IOCfg)
    site: SiteCfg = field(default_factory=SiteCfg)
    persistence: PersistenceCfg = field(default_factory=PersistenceCfg)
    health: HealthCfg = field(default_factory=HealthCfg)


def _coerce(cls, data: dict[str, Any]):
    """Best-effort dict → dataclass, recursing one level for nested types."""
    out = cls()
    for k, v in (data or {}).items():
        if not hasattr(out, k):
            continue
        attr = getattr(out, k)
        if hasattr(attr, "__dataclass_fields__") and isinstance(v, dict):
            setattr(out, k, _coerce(type(attr), v))
        else:
            setattr(out, k, v)
    return out


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config not found: {p}")
    with p.open() as f:
        raw = yaml.safe_load(f) or {}
    return _coerce(Config, raw)
