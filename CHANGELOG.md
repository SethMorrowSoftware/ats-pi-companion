# Changelog

All notable changes to the ATS-Pi companion service are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The implemented ICD contract version is independent of the Python
package version — see `atspi.ICD_VERSION` for the wire-protocol version.

## [Unreleased]

### Added

- Strict config loader: unknown keys now raise `ConfigError` with the
  dotted path of the offending key, instead of silently using defaults.
- `_GuardedSlaveContext` rejects writes to any address outside the four
  ICD command registers (`0x0100`–`0x0103`) with a Modbus exception
  instead of silently succeeding.
- Per-command mode policy enforcement on Modbus writes; rejections
  latch `FAULT_INPUT` in `fault_summary` until cleared by the next
  valid command (ICD §write response contract).
- `_wait_for_shutdown_or_failure` races SIGTERM/SIGINT against the
  critical-task set; service exits non-zero on background-task death
  so systemd `Restart=on-failure` triggers immediately.
- `tests/test_main.py`, `tests/test_io_adam.py::test_*_idempotency`,
  `tests/test_state.py::test_mode_*`, and end-to-end Modbus write
  rejection test in `tests/test_server.py`.
- Dependabot config for weekly pip + GitHub Actions updates.
- `pip-audit` job in CI (informational; doesn't gate merges).

### Changed

- `apply_input_snapshot` no longer overwrites the locally-managed
  `FAULT_INPUT` / `FAULT_OUTPUT` bits — they survive across sampling
  cycles until explicitly cleared.
- `IOMockDriver._pulse` and `IOAdamDriver._pulse` enforce ICD pulse
  idempotency: writes during an active pulse are ignored.
- Persistence writes are offloaded to the asyncio executor; the
  10 Hz sampling loop no longer blocks on `fsync` (50–200 ms on
  microSD).
- Atomic-rename persistence also fsyncs the parent directory so the
  rename itself survives power loss.
- `IOAdamDriver._ensure_connected` closes and recreates the pymodbus
  client after a failed read/write rather than reusing a potentially
  half-open socket.
- `start_server` waits for the listening socket to accept connections
  rather than a fixed `asyncio.sleep(0.1)`.
- `logging.basicConfig` runs before config load and driver/store
  construction so early messages land in the configured handler.
- systemd unit uses `StateDirectory=atspi` (auto-creates `/var/lib/atspi`
  with correct perms) and gains additional hardening flags
  (`ProtectClock`, `RestrictNamespaces`, `SystemCallFilter`, …).
- CI: added `concurrency:` cancel-in-progress, `fail-fast: false` in
  the matrix.
- `pymodbus` pin loosened from `==3.7.4` to `~=3.7.4` (patch-only).
- `Development Status` classifier: Alpha → Beta.

### Fixed

- `OUTPUT_FAULT` set by a failed `drive_outputs` is no longer cleared
  by the next successful input read.
- Modbus writes with invalid values to allowed registers, and writes
  to reserved or read-only addresses, now return a Modbus exception
  to the client.

## [0.1.0] – initial scaffold

- Register store implementing the ICD §5 layout.
- Mock and ADAM-6060 I/O drivers.
- Modbus TCP server backed by pymodbus.
- Safety watchdog (30 s comms-loss auto-release per ICD §8.3).
- Atomic JSON persistence for lifetime counters.
- systemd `Type=notify` unit with `WatchdogSec=60`.
- CI on Python 3.11 and 3.12.
