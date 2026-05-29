# Changelog

All notable changes to the ATS-Pi companion service are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The implemented ICD contract version is independent of the Python
package version — see `atspi.ICD_VERSION` for the wire-protocol version.

## [Unreleased]

### Fixed (pre-hardware reliability sweep)

- `IOAdamDriver` no longer strands a pulsed relay (Test, Bypass) asserted
  when the release write fails. Previously, if a network/ADAM blip landed
  on the exact instant a pulse was released, the release write raised, the
  fire-and-forget release task died unretrieved, and nothing retried — the
  relay stayed energised. For the Test output that means continuously
  commanding the ATS to test-transfer to the generator; for Bypass it
  defeats every transfer time delay. Worse, stuck-relay detection could not
  see it: the driver still believed it had commanded the relay ON, so
  commanded==actual==True and no `OUTPUT_FAULT` was raised. The release now
  (a) records the intended OFF state at pulse expiry — so an overstaying
  relay surfaces as `OUTPUT_FAULT` past the settling window — and (b) retries
  the release write until it lands, mirroring the safety watchdog's
  "retry until the write lands" posture for maintained commands.
- `RegisterStore.apply_input_snapshot` now gates transfer counting on a
  plausible predecessor position instead of "any position that isn't the
  destination". Two production-only bugs the unit suite's direct
  utility↔generator transitions never exercised:
  - **Lifetime count drifted up on reboot.** Boot position defaults to
    `unknown`, so a first read landing on `generator` (a restart during a
    utility outage) counted as a fresh transfer — `transfer_count_lifetime`
    would climb by one on every reboot while the ATS sat on the generator.
    A momentary both-aux-open glitch (reads as `unknown`) bouncing back to
    the same rail double-counted the same way. Transfer-to-gen now counts
    only from `utility`/`transferring`; retransfer-to-util only from
    `generator`/`transferring`.
  - **`last_retransfer_to_util_ts` was never stamped on real hardware.** The
    retransfer stamp required the position seen immediately before `utility`
    to be exactly `generator`, but the Load Disconnect pulse holds the
    position at `transferring` for ~2 s through the stroke — so the real
    `generator → transferring → utility` path left the timestamp at 0
    forever. Including `transferring` as a valid predecessor fixes it.

### Fixed (commissioning-day robustness)

- `IOAdamDriver` now passes `timeout=0.5` and `retries=1` to
  `AsyncModbusTcpClient`. pymodbus's defaults (3 s × 3 retries =
  up to 9 s per operation) would stall the 10 Hz sampling loop on any
  flaky Ethernet drop, and the service would look wedged to an
  operator. With these values a hard failure surfaces in ~1 s and the
  next sampling cycle retries 100 ms later.
- `config.example.yaml`: prominent banner reminding operators to flip
  `driver: mock` → `driver: adam` before production deploy. With mock
  the service reads from RAM and reports a constant healthy snapshot
  forever — easy to miss because it 'works'.
- `systemd/atspi.service`: comment now spells out the
  `User/Group resolution: 'atspi' not found` failure mode and
  cross-references the commissioning sequence.
- `docs/HARDWARE.md`: new §7 commissioning checklist with the literal
  command sequence from "Pi configured" to "GenWatch sees the ATS",
  plus a symptom-to-fix table for the five most likely first-boot
  gotchas.
- `docs/RUNBOOK.md`: §1 notes `modpoll` isn't in Raspbian's base
  package set (`sudo apt install modbus-cli`); §3(a) distinguishes a
  transient network blip from a real wiring fault under the new
  500 ms timeout.

### Fixed (time-source correctness)

- `ats_pi_uptime_s` (`0x0014`) now derives from `time.monotonic()`, not
  `time.time()`. The old wall-clock source meant any NTP correction
  backward (or manual clock adjustment) made uptime decrease — which
  ICD §6.2 + §7.3 explicitly reserves as the "undetected reboot"
  signal. GenWatch would spuriously fire `ATS_PI_REBOOT` events on any
  large NTP correction.
- u32 register reads now pin a single timestamp for the whole multi-
  word read. Previously `uptime_s` and `wallclock` each called
  `time.*()` separately for the high and low word — at every 65 536 s
  boundary (the high-word transition) the two halves could straddle
  the wrap and reconstruct to a value off by `0x10000` (≈18 hours of
  drift). GenWatch's `TIME_SKEW` alarm would fire on the next prime
  poll after the boundary.
- `docs/SPEC.md` removed the stale `logging.level` config example —
  log level is set via the `--log-level` CLI flag, and the strict
  config loader would reject a `logging:` section anyway.

### Added (ICD contract conformance pass)

- `tests/test_icd_contract.py`: 44 end-to-end tests against a real
  pymodbus client. Asserts register layout, u32 word order, boolean /
  enum / bitfield encoding, mode-policy enforcement on the wire, the
  ICD §10 golden transfer-and-retransfer sequence, write-reply latency
  (< 100 ms), and atomicity of multi-word reads under concurrent state
  updates. Catches contract drift on the ATS-Pi side; the
  complementary "GenWatch consumer matches the ICD" test must live in
  the GenWatch repo.
- `RegisterStore.can_write(addr)`: pre-validates a holding-register
  write for the Modbus `validate()` hook. Returns False (and latches
  `mode_reject_active`) on mode-policy violation, so the rejection
  surfaces as a Modbus exception response rather than silent success.

### Fixed (ICD compliance)

- Mode-policy violations now return a Modbus exception to the client
  rather than silently succeeding. Mode enforcement moved from
  `RegisterStore.write_register` to `_GuardedSlaveContext.validate`,
  which is the only pymodbus 3.7 hook that can emit an exception
  response.
- Reads of reserved addresses through `0xFFFF` now return `0x0000`
  per ICD §3. Previously addresses past the data block's allocated
  `0x0200` size returned exception 0x02 (IllegalAddress). The
  `validate()` override now accepts any read address; `getValues`
  delegates to `RegisterStore.read_register`, which already returns
  0 for unknown addresses.
- `fault_summary` (`0x0005`) reads are masked to `0x000F`: ICD §1.1.1
  says bits 4-15 are RESERVED and MUST be 0 on the wire. A buggy
  driver reporting stray bits in `InputSnapshot.fault_bits` can no
  longer leak them to GenWatch.

### Known ICD deviations (documented, not yet fixed)

- Reserved-range write rejection returns Modbus exception 0x02
  (illegal data address) instead of the ICD-preferred 0x03 (illegal
  data value).
- Mode-policy rejection returns Modbus exception 0x02 instead of the
  ICD-preferred 0x04 (server device failure).

Both are pymodbus 3.7 limitations — `validate()` is the only hook that
can emit a Modbus exception response and it can only emit 0x02. The
safety property the ICD actually cares about (write rejected with a
Modbus exception, GenWatch knows it didn't take effect) holds in both
cases. Both client-side workarounds — treat any exception as
rejection, optionally consult `fault_summary` for the distinction —
are trivial. Reaching exact code compliance requires either porting to
a newer pymodbus minor (which has its own datastore-API rewrite cost)
or a custom request handler.

### Fixed (trunk regression sweep)

- `__main__._amain` lost the call to `_wait_for_shutdown_or_failure` in
  a merge; the service printed "running" then immediately "shutting
  down" and NameError'd on an undefined `reason` variable. Restored the
  call so SIGTERM and critical-task death both drive a clean exit.
- `pyproject.toml` pinned back to `pymodbus>=3.7.4,<3.8`. The previous
  `<3.14.0` ceiling let pymodbus 3.13 install, which renamed
  `ModbusSlaveContext`→`ModbusDeviceContext` and broke `server.py`'s
  imports. Bumping past 3.7.x now requires porting the datastore code
  first.
- `tests/test_server.py` lost `import pytest`, `import asyncio`,
  `_GuardedSlaveContext`, and `start_server` in a merge conflict
  resolution — the file no longer collected. Restored.
- `tests/test_state.py` had a duplicate `test_write_register_returns_command_intent`
  and three tests calling an undefined `_store_in_auto`. Replaced the
  stub with the real helper.
- `SafetyWatchdog.run` now latches `_released=True` only when the
  physical release write succeeds. Previously a transient driver
  failure during a comms-loss event would leave inhibit / force-
  transfer asserted on the ADAM until comms recovered. Retries every
  `CHECK_INTERVAL_S` until the write lands.
- `IOAdamDriver._pulse` had a dead branch that recomputed `slot` and
  attempted to cancel a prior release task on a code path that could
  only run when no prior task existed. Removed.

### Added

- `SafetyWatchdog.snapshot()` returns `(last_read_age_s, released)` as
  a stable shape for the health endpoint and future metrics consumers
  (replaces poking at private attributes).
- CI `soak` job: starts atspi against the mock driver, performs a real
  Modbus read with pymodbus, sends SIGTERM, and asserts exit 0. Would
  have caught the `_wait_for_shutdown_or_failure` regression.
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
