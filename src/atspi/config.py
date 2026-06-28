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
    # High port, NOT the privileged Modbus 502: the service runs as the non-root
    # `atspi` user (no CAP_NET_BIND_SERVICE) and cannot bind 502. GenWatch's
    # ats.port defaults to 5020 to match. See config.example.yaml / HARDWARE §4.1.
    port: int = 5020
    unit_id: int = 1


@dataclass
class HwWatchdogCfg:
    """ADAM-6000 host-watchdog / DO safety-value readback config (F1).

    Where to read the hardware fail-safe config back from and what counts as
    "armed". Addresses are PDU offsets (0-based) and are BENCH-VERIFY: they
    live in the ADAM-6000 User Manual (Appendix B) and vary by firmware, so
    they default to unset (``null``) — with ``require_hw_watchdog: true`` an
    unset/unverifiable check fails closed (outputs refused). See HARDWARE.md
    §5.1 and io_adam.HwWatchdogConfig.
    """
    enable_register: int | None = None
    enable_expected: int = 1
    timeout_register: int | None = None
    timeout_scale_s: float = 0.1
    timeout_min_s: float = 5.0
    timeout_max_s: float = 10.0
    safety_value_register_base: int | None = None
    safety_value_count: int = 6


@dataclass
class AdamCfg:
    host: str = "192.168.1.251"
    port: int = 502
    unit_id: int = 1
    # Consecutive identical 10 Hz samples a level input must hold before the
    # driver publishes the change (rejects contact bounce / EMI). 1 disables.
    debounce_samples: int = 3
    # ATS mode the driver reports (the ADAM has no Auto/Manual sense contact).
    # Also gates command writes per ICD §6: 'auto' allows all, 'manual' allows
    # only inhibit, 'test'/'unknown' block all. One of: auto|manual|test|unknown.
    assumed_mode: str = "auto"
    # Modbus function code used to read the 6 DIs: 'coils' (FC01) or
    # 'discrete_inputs' (FC02). Confirm on the bench — see io_adam.VALID_DI_READS.
    # Default keeps FC01 (current behaviour); flip to 'discrete_inputs' if the
    # DIs read all-0 / position stays 'unknown'. One of: coils|discrete_inputs.
    di_read: str = "coils"
    # F1 — require the ADAM hardware host-watchdog / DO safety-value fail-safe to
    # be verified armed before the driver will assert any output. Default-on:
    # this is the single safety-critical gate before driving a real switch. Set
    # false ONLY as an explicit, auditable waiver for bench work. When true, the
    # hw_watchdog register addresses below must be configured and bench-verified.
    require_hw_watchdog: bool = True
    # Second, explicit acknowledgement required to run with the F1 fail-safe
    # waived (require_hw_watchdog: false). Without the ADAM readback gate AND
    # without a Pi-level hardware watchdog there is NO automatic release if the
    # Pi process dies with a relay latched — only the procedural cable-pull
    # test. Refusing to start unless the operator explicitly sets this keeps the
    # one-line `require_hw_watchdog: false` from silently removing the last
    # crash-time backstop. See __main__._enforce_hw_watchdog_waiver / HARDWARE §5.
    i_understand_no_crash_backstop: bool = False
    hw_watchdog: HwWatchdogCfg = field(default_factory=HwWatchdogCfg)


@dataclass
class AscoSerialCfg:
    """ASCO Group 5 RS-485 Modbus RTU reader config (used when driver: hybrid).

    Serial link params + the Group 5 holding-register / bit map that the
    ``hybrid`` driver reads position and source availability from, in place of
    the 18RX / 14AA-14BA accessories. Bit indices are flat across the read
    block (bit b → register status_register + b//16, bit b%16). The status
    register and the four required bits are BENCH-VERIFY from ASCO doc
    381339-221 and default to unset — the hybrid driver refuses to start until
    they are supplied (io_asco_serial.AscoSerialConfig.validate). See
    HARDWARE.md §3.1.
    """
    # Stable udev symlink created by udev/99-atspi-serial.rules (install.sh
    # deploys it). Do NOT default to a raw /dev/ttyUSB0 — that index is not
    # stable across reboot/re-plug and silently breaks ASCO sensing.
    port: str = "/dev/atspi-asco"
    baudrate: int = 19200
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1
    unit_id: int = 1  # controller RS485 address (1-247)
    timeout_s: float = 1.0
    # ATS mode reported in the snapshot / command gate (ICD §6); same semantics
    # as io.adam.assumed_mode. One of: auto|manual|test|unknown.
    assumed_mode: str = "auto"
    # Group 5 holding-register status map (FC03). PDU offsets / flat bit indices.
    status_register: int | None = None
    status_register_count: int = 1
    on_normal_bit: int | None = None
    on_emergency_bit: int | None = None
    normal_available_bit: int | None = None
    emergency_available_bit: int | None = None
    transferring_bit: int | None = None
    engine_start_bit: int | None = None


@dataclass
class IOCfg:
    driver: str = "mock"  # 'mock' | 'adam' | 'hybrid'
    adam: AdamCfg = field(default_factory=AdamCfg)
    # Used when driver: hybrid (serial monitoring + ADAM control). The output
    # side still reads io.adam.* — only the input path comes from io.asco_serial.
    asco_serial: AscoSerialCfg = field(default_factory=AscoSerialCfg)


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
