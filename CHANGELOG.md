# Changelog

All notable changes to the ATS-Pi companion service are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The implemented ICD contract version is independent of the Python
package version — see `atspi.ICD_VERSION` for the wire-protocol version.

## [Unreleased]

### Fixed / hardened (post-audit safety pass, 2026-06)

- **CRITICAL (C-1): the comms-loss auto-release is now scoped to GenWatch's
  connection (ICD §3/§8.3).** The watchdog previously re-armed on *any*
  successful Modbus read, so a diagnostic `modpoll`, a scanner, or a stale
  second GenWatch on a separate connection could indefinitely suppress the
  30 s auto-release of a latched force-transfer/inhibit. A `ConnectionTracker`
  now identifies the authoritative connection as the one that issues command
  writes (only GenWatch commands the switch; diagnostic tools read) — only that
  connection's activity re-arms the watchdog, and a drop of that connection
  triggers an immediate release. No wire-protocol change (ICD v1.0 unchanged).
  New end-to-end tests (`test_c1_connection_scoping.py`) prove a busy
  diagnostic reader cannot keep a latched command alive.
- **Transfer-count durability (M-4, ICD §8.2).** The `transfer_count_lifetime`
  increment is now persisted **synchronously** before the bumped value becomes
  observable over Modbus. Previously the count was published to the read
  registers while the fsync was offloaded to the loop executor, so a power cut
  in that window left the post-reboot count one lower than GenWatch last saw —
  a backwards jump that violates the monotonicity contract. Routine (non-count)
  persists are still offloaded.
- **F1 fail-safe waiver now requires an explicit second acknowledgement (H-1).**
  Setting `io.adam.require_hw_watchdog: false` removes the only software gate on
  driving outputs; combined with a process death it can strand a latched relay.
  The service now refuses to start unless `io.adam.i_understand_no_crash_backstop:
  true` is also set — a one-line waiver can no longer silently remove the
  crash-time backstop. `config.production.example.yaml` sets the ack (the 6060's
  FSV/WDT isn't Modbus-readable).
- **Pi-level hardware watchdog (H-1).** Added
  `systemd/system.conf.d/10-atspi-hwwatchdog.conf` (and `install.sh` deploys +
  verifies it) so a kernel/USB hang hard-resets the Pi — the software watchdog
  can't restart pid 1. The companion is the device that physically commands the
  switch and previously had no Pi-level watchdog (GenWatch did).
- **Stable serial device path (H-6).** Added `udev/99-atspi-serial.rules`
  (deployed + reloaded by `install.sh`) creating a stable `/dev/atspi-asco`
  symlink for the Waveshare USB-RS485 adapter; the config default is now that
  symlink, not a raw `/dev/ttyUSB0` that moves across reboot/re-plug and
  silently broke ASCO sensing.
- **Hash-pinned dependencies (H-7).** Added `requirements.lock` (pip-compile
  `--generate-hashes`); `install.sh` installs deps with `--require-hashes` and
  the package `--no-deps`, and refuses to install without the lock (override
  with `ATSPI_ALLOW_UNPINNED=1`). The two Pis and every re-image now get
  byte-identical, tamper-evident wheels.
- **NTP enforced at install (M-3).** `install.sh` enables time sync and reports
  sync status — the ICD <5 s skew is a hard contract and the Pi has no RTC.
- **systemd unit hardened (M-1, M-2).** `Restart=always` +
  `StartLimitIntervalSec=0` (an unattended safety device must never latch in
  `failed` requiring on-site `reset-failed`); `OOMScoreAdjust=-200` and memory/
  task/fd caps; and `MemoryDenyWriteExecute` removed (it can SIGSYS-crash some
  CPython builds — re-enable only after validating on the target image).

### Added (hybrid / serial commissioning — the Waveshare RS-485 path is now deployable)

- **The `driver: hybrid` path (ADAM-6060 control + ASCO Group 5 read over a
  USB-RS485 adapter) is now documented and runnable end-to-end.** The driver
  code already existed and fails closed, but a deployer couldn't get there: the
  commissioning docs assumed the contact-only `adam` path and the service
  couldn't even open the serial port.
  - **Serial-device access (hard blocker).** The non-root `atspi` service user
    had no access to `/dev/ttyUSB0`, so the serial half silently never came up
    as a service (bench `modpoll` as root worked, hiding it). The systemd unit
    now sets `SupplementaryGroups=dialout` and `install.sh` adds `atspi` to
    `dialout`.
  - **`HARDWARE.md §3.1`** gains the Pi-side bring-up that was missing: confirm
    the serial device node (`lsusb`/`dmesg`, FTDI vs CH34x, `ttyUSB0` vs
    `ttyACM0`), the A/B(+/−)/GND wiring procedure (swap-if-silent, 120 Ω
    termination), the `dialout` requirement, and the consequence of leaving the
    optional `transferring`/`engine_start` bits unset.
  - **The commissioning narrative now branches for hybrid:** `TUTORIAL.md`
    (driver-path callouts in the bench/config/transition phases), `BENCH.md`
    (scope banner; the DI checks marked adam-only), `NEXTSTEPS.md` (the
    DI-jumper table is contact-path-only — hybrid drives the controller),
    `FIELD-INSTALL.md` (a `Step 3-H` that lands the single USB-RS485 run instead
    of six DI contacts), `SIGN-OFF.md` (a `driver:` field + an `io.asco_serial`
    bench-verified record), `RUNBOOK.md` (serial INPUT_FAULT signature; the
    wrong-map fix is a bench-verify, not a `di_read` flip), and `README.md` /
    `DEVELOPMENT.md`. The entire control/server side (F1 cable-pull, comms-loss
    release, mode policy, the 5020 server) is identical and unchanged.
  - `RUNBOOK.md` fault table: `CALIBRATION` (bit 3) is no longer "Reserved" — it
    is now emitted (both position-sense inputs asserted at once), so the table
    states its real meaning; `config.example.yaml` spells out the optional-bit
    consequences inline.

### Fixed (commissioning plan — Modbus server port reconciled to 5020)

- **Every doc, config default, and acceptance check now uses port `5020` for
  the Modbus server GenWatch reads — not the privileged `502`.** The service
  runs as the non-root `atspi` user with no `CAP_NET_BIND_SERVICE`, so it
  cannot bind `502`; the production config already shipped `5020` and GenWatch's
  client defaults to `5020`, but `TUTORIAL.md`, `NEXTSTEPS.md`, `HARDWARE.md`,
  `SPEC.md`, `README.md`, the `config.example.yaml` / `config.py` dev defaults,
  and the `nmap` / `modpoll` / firewall snippets still said `502`. A literal
  follower either crash-looped the service (binding `502`) or pointed GenWatch
  at a dead port (service on `5020`, client on `502`) — and three of the five
  acceptance gates (ICD, §8.3, F3) couldn't close, with the `nmap -p 502` check
  passing for the wrong reason. The ADAM-target `502` (the `io.adam.port`,
  `atspi-bench` / `testadam.sh`, the `ats-pi → ADAM` firewall rule, the ADAM's
  factory `10.0.0.1:502`) is unchanged — only the service-listen port moved.
  `test_smoke.py` now asserts the `5020` default.

### Added (safety-critical — CALIBRATION fault for impossible contact states)

- **Both position-sense inputs asserted at once now sets `fault_summary` bit 3
  (CALIBRATION, ICD §5.1.1).** A transfer switch cannot be on both sources at
  once; `on_normal` and `on_emergency` both closed is a welded/miswired aux
  contact (or status bit). Both real readers already collapsed that to
  `position=unknown` — but with `fault_summary=0`, indistinguishable from a
  legitimate both-off mid-stroke, so GenWatch got no diagnostic. The bit was
  defined and plumbed end-to-end (mask, health decoder, store pass-through) but
  **no code ever set it**; `io_adam` and `io_asco_serial` now raise it (off the
  debounced / decoded bits, so a transient can't trip it) while still reporting
  `position=unknown`. Covered by new tests in `test_io_adam.py` and
  `test_io_hybrid.py`. The fault-bit constants moved to the I/O boundary
  (`io_driver`) and are re-exported from `atspi.state` — no import changes for
  existing consumers.

### Changed (operability + doc accuracy)

- **The F1 hardware-fail-safe waiver is now logged loudly at startup.** When
  `require_hw_watchdog: false` on a real `adam` / `hybrid` driver (mandatory on
  the ADAM-6060, which can't expose its FSV/WDT over Modbus), the readback gate
  is silently waived — nothing in software verifies the fail-safe. Startup now
  emits a WARNING that F1 is procedural and the §5.1 cable-pull is the only
  proof, to be re-run after any ADAM swap or factory reset.
- **Corrected the `OUTPUT_FAULT` → GenWatch overclaim.** `SPEC.md §5`,
  `config.example.yaml`, and the `io_driver` / `__main__` docstrings said a
  published `OUTPUT_FAULT` makes GenWatch "see a non-authoritative link and
  refuse commands." GenWatch's authority gate keys only on comms health, ICD
  *major* version, and `unit_id`; `fault_summary` bits are warn-only alarms.
  The refusal that actually blocks the relay is enforced *locally* by this
  driver — the docs now say so.
- **`RUNBOOK.md §11` rollback** no longer `cd`s into a path `install.sh` never
  creates, nor runs a system-wide `pip install`; it re-runs `install.sh` (the
  documented idempotent upgrade/rollback path, matching the venv model).
- **`README.md` test count** refreshed — the headline figure had drifted from
  the actual suite.

### Added (`hybrid` driver — serial monitoring without 18RX / aux contacts)

- **`driver: hybrid` reads ATS state from the ASCO Group 5 controller over
  RS-485 Modbus RTU, while control stays on the ADAM-6060.** The contact-only
  path (`driver: adam`) needs the 18RX REX module and 14AA/14BA aux contacts
  (~$800) because the ASCO's 16-terminal customer strip exposes only one of the
  six sense inputs (Load Disconnect). The hybrid driver reads position and
  source availability straight from the controller's serial port via a ~$12
  USB-RS485 adapter, so those accessories aren't needed. New modules
  `io_asco_serial.py` (`AscoSerialReader`, an RTU reader that decodes a
  configurable Group 5 holding-register/bit map into an `InputSnapshot`) and
  `io_hybrid.py` (`IOHybridDriver`, composes the reader's inputs with an
  `IOAdamDriver` for outputs). Every output-side method — including the F1
  `hw_watchdog` gate and stuck-relay `check_output_consistency` — delegates to
  the ADAM driver unchanged, so the control-side safety behaviour is identical
  to `driver: adam`.
- The Group 5 register/bit addresses are **BENCH-VERIFY from ASCO doc
  `381339-221`** and default to unset; the driver **refuses to start** until
  `status_register` and the four required bits are configured (it will not
  publish a guessed switch position for a live switch) — the same fail-closed
  discipline as the ADAM `di_read` / `hw_watchdog` addresses. New config block
  `io.asco_serial` (strict-loader validated), wiring/commissioning guidance in
  `docs/HARDWARE.md §3.1`, and `pyserial` added as an explicit dependency
  (pymodbus lists it only as an extra). Covered by `tests/test_io_hybrid.py`
  (20 tests: config fail-fast, bit/position decode, multi-register blocks, read
  errors, and hybrid output delegation incl. the preserved F1 gate).

### Added (docs — turnkey field install packet)

- **`docs/FIELD-INSTALL.md`, `docs/SIGN-OFF.md`, and
  `config.production.example.yaml`** — a cabinet-install packet so the wiring
  electrician needs no other doc on site. The field card renders the §3 terminal
  map as a checkbox-per-wire table (power, network, DI/DO landings), flags the
  parallel-sense engine-start input (DI5) and the factory-use ASCO terminals
  14–16, and walks read-side-first verification before any relay is driven. The
  sign-off sheet is a fill-in acceptance record for the F1/F3/F4/ICD/§8.3 gates.
  The production config pre-locks the bench-verified 6060 settings (`di_read:
  coils`, `require_hw_watchdog: false`) and keeps port `5020` — the service runs
  as the non-root `atspi` user with no `CAP_NET_BIND_SERVICE`, so it cannot bind
  the privileged 502 — leaving only the site IPs to fill in.

### Added (docs — single end-to-end tutorial)

- **`docs/TUTORIAL.md`: a linear, follow-along walkthrough of the whole job**,
  from parts on the bench through wiring the ASCO to seeing state in GenWatch.
  The detailed procedures already lived in `BENCH.md`, `NEXTSTEPS.md`, and
  `HARDWARE.md`, but in three separate files with a safety-critical ordering
  (bench → staging → cabinet) that a first-time integrator had to infer. The
  tutorial is a thin orchestration layer over those canonical docs — it
  sequences the six phases, calls out each acceptance gate (F1/F3/F4/ICD/§8.3),
  reproduces the ASCO terminal-mapping table inline (canonical source still
  `HARDWARE.md §3`), and links each phase to its reference doc rather than
  duplicating the deep detail. Linked from the README docs index and the
  Production-deployment section as the recommended starting point.

### Added (safety-critical — ICD §9.3 reset-on-reboot now actively enforced)

- **All four command outputs are driven OFF at service startup and on
  graceful shutdown** (`IODriver.release_all_outputs`). ICD §9.3 requires
  command registers to start in the no-commands-asserted state after a
  reboot, but only the in-memory store reset implemented that — the
  *physical relay* kept whatever the previous instance last wrote. A
  `systemctl restart` completes in ~2 s, faster than the ADAM host-idle
  watchdog's 5–10 s window, so a maintained Inhibit / Force-Transfer relay
  (or a Test/Bypass pulse whose release timer died with the old process)
  latched by the previous instance would survive into the new boot with
  nothing left to release it: the new instance's read-back honestly showed
  it asserted, the comms-loss watchdog saw healthy GenWatch polling and
  never fired, and the hardware fail-safe never saw host silence. Now:
  - The sampling loop's first action is `release_all_outputs()` (Test,
    Force-Transfer, Inhibit, Bypass → OFF; spares untouched), retried every
    cycle until the write lands (e.g. ADAM unreachable at boot), before any
    input state is published.
  - The same release runs on graceful shutdown (SIGTERM / `systemctl
    stop`), bounded by a 5 s timeout, so an orderly stop doesn't leave a
    relay latched for the hardware watchdog to clean up — or latched
    indefinitely where that fail-safe isn't configured.
  - Releases are exempt from the F1 hardware-fail-safe gate (same rule as
    the comms-loss watchdog), so the reset also runs while the gate is
    failing closed.
  - `atspi-bench`'s run-exit safety net now uses the same call, fixing a
    real gap: a Ctrl-C during a Test/Bypass *pulse* cancelled the pulse's
    release timer on close, stranding the Test relay energised — on a bench
    module with no FSV configured, indefinitely. (The previous net released
    only the two maintained relays.)
  - Covered by new tests in `test_io_adam.py`, `test_main.py`,
    `test_bench.py`, and `test_smoke.py`.

### Changed (docs — ADAM-6060 F1 readback reality)

- **`HARDWARE.md §5.2` and `config.example.yaml` now carry the ADAM-6060
  bench finding** (`BENCH.md §10` finding 4): the 6060 does not expose its
  FSV/Communication-WDT config over Modbus, so `require_hw_watchdog: true`
  can never pass on that model — it must be `false`, the §5.1 cable-pull
  test is the F1 acceptance gate, and the cable-pull must be re-run after
  any ADAM swap or factory reset. Previously §5.2 and the example config
  read as if the readback was expected to work, which would dead-end a
  commissioning that followed them literally.

### Added (safety-critical — F1: ADAM hardware fail-safe now verified by software)

- **The ADAM-6060 driver reads the host-watchdog / DO safety-value config back
  on connect and refuses to drive the switch unless the hardware fail-safe is
  armed.** The ADAM's host-idle watchdog + per-DO safety values are the only
  thing that releases a latched Force-Transfer / Inhibit relay if the *Pi
  itself* dies (the `safety.py` software watchdog shares fate with the
  process). That backstop was configured by hand (`HARDWARE.md §5.1`) with
  nothing confirming it stayed configured — skip the step, or factory-reset /
  swap the ADAM, and the only safety net was silently gone.
  - New `io.adam.require_hw_watchdog` (**default `true`**). On connect the
    driver reads the watchdog-enable, watchdog-timeout, and per-DO
    safety-value registers and treats the fail-safe as armed only if the
    watchdog is enabled, the timeout is in the 5–10 s band, and every DO
    safety value is `0`/OFF (matching `HARDWARE.md §5.1`).
  - **Fails closed.** If the check fails — disabled, wrong timeout, a non-zero
    safety value, registers unconfigured, or a read error — the driver refuses
    to *assert* any output (`HwWatchdogNotArmedError`) while still allowing
    *releases* (so the comms-loss watchdog and bench cleanup can drop relays),
    and the sampling loop publishes a persistent `OUTPUT_FAULT` so GenWatch's
    authority gate sees a non-authoritative link and refuses commands. The
    reason is logged loudly at startup. Never a silent arm.
  - The watchdog register addresses are **bench-verify config**
    (`io.adam.hw_watchdog.*`, PDU offsets) sourced from the *ADAM-6000 Series
    User Manual* (Appendix B) — the same discipline as `io.adam.di_read`. They
    are left unset by default, so commissioning can't proceed until they're
    filled in and the `§5.1` cable-pull test passes (the cable-pull, recorded
    on the sign-off, remains the real acceptance gate — a wrong address can
    read an armed-looking value). Documented in `HARDWARE.md §5.2`.
  - `atspi-bench` waives the gate internally (`require_hw_watchdog=false`) since
    it drives outputs on the bench under LOTO; that waiver is the only intended
    use of `false`. Covered by new unit tests in `tests/test_io_adam.py` and a
    sampling-loop fault test in `tests/test_main.py`.

### Changed (deployment boundary — F3: Modbus command server segmentation)

- **Documented the Modbus TCP server's network boundary as a safety control.**
  Modbus/TCP has no authentication; the server whitelists the four command
  registers and enforces mode policy, but anything that can route to port 5020
  can command within that policy. `config.example.yaml` and new `HARDWARE.md
  §4.1` now frame OT-VLAN segmentation + a firewall allowlist (only `GenWatch
  Pi → ats-pi:5020`, `ats-pi → ADAM:502`) and binding `modbus_server.host` to
  the OT-side interface (not `0.0.0.0`) as a deliberate, documented control,
  with an `nmap`-filtered acceptance check. No transport auth is added (none
  exists in Modbus/TCP); enforcement is delegated to the host firewall.

### Added (release discipline — F4: commission a pinned build)

- **`HARDWARE.md §9` — pin and tag the commissioned build.** The test suite
  proves internal consistency against a simulation, not that the ADAM/ASCO
  mappings are right for a specific unit. Documented cutting a tagged release
  candidate (e.g. `v0.1.0-rc1`) at the commissioning commit, deploying only
  that tag, commissioning it with the matching GenWatch tag, and recording the
  tag + F1 cable-pull + watchdog-readback config + F3 `nmap` check on the
  sign-off sheet.

### Fixed (ICD conformance)

- **FC23 (read/write multiple registers) writes are now rejected.** The
  `_GuardedSlaveContext.validate()` guard gated FC06/FC16 and coil writes but
  not FC23 (`0x17`), which fell through to the bounds-only default `validate()`
  — so an FC23 request that wrote a read-only or reserved address returned `OK`
  instead of a Modbus exception, violating the ICD §6.1
  reject-undefined-writes contract. (The write was still functionally dropped
  by `RegisterStore.write_register`, so no relay was driven — only the wire
  response was wrong.) FC23 is now rejected wholesale with exception 0x02, like
  coil writes: pymodbus validates its read-range and write-range under the same
  function code so the write-range can't be gated independently, and the ATS-Pi
  doesn't define FC23 anyway (clients read via FC03/FC04, write via FC06/FC16).
  Covered by a unit test and an end-to-end round-trip in `tests/test_server.py`.

### Added (ADAM-arrival hardening)

- **`io.adam.di_read` config (FC01 ↔ FC02 DI read).** The driver read the 6
  digital inputs with `read_coils` (FC01); on the ADAM-6000 series the
  *documented* DI mapping is `read_discrete_inputs` (FC02) — FC01 reads the
  *relay outputs*. Whether a given unit/firmware also mirrors the DIs into the
  coil space is firmware-dependent and can only be confirmed on the bench. The
  DI function code is now an operator-settable value (`coils` |
  `discrete_inputs`, default `coils` = unchanged behaviour), so a wrong guess
  is a one-line config flip at commissioning instead of a code edit + redeploy.
  Threaded through `atspi-bench --di-read` and `testadam.sh --di-read` so both
  function codes can be tried interactively. Symptom of the wrong choice (all
  DIs read 0 / `position` stuck `unknown`, no `INPUT_FAULT`) and the fix are in
  HARDWARE.md §3, the §7 gotcha table, and RUNBOOK §2 step 5. The relay outputs
  are unchanged — always coils (FC01 read-back / FC05 write).
- **`docs/HARDWARE.md §5.1` — ADAM-6060 host watchdog / DO fail-safe.** The
  software safety watchdog (ICD §8.3) only releases maintained commands while
  the Pi is alive; it cannot fire if the Pi loses power or panics with Force
  Transfer / Inhibit asserted, leaving the ADAM latching the relay forever.
  Documented configuring the ADAM's own host-idle watchdog with per-DO safety
  values (all DOs → OFF, 5–10 s timeout) as a required, verified commissioning
  step — the hardware backstop for host death. Cross-referenced from SPEC §5,
  RUNBOOK §5, and the §8 safety reminders.
- **Startup warning when `site.unit_id` is left at the default `1`.** The
  default `site.unit_id` is `1` (also the ADAM's factory-default address),
  while `config.example.yaml` uses `23`. A deployment that omits the `site:`
  block makes register `0x0035` report `1`; GenWatch pins this via its
  `expected_unit_id` check (ICD §5.4) and refuses authority on a mismatch,
  which surfaces as a confusing "authority refused" rather than an obvious
  misconfiguration. Startup now logs a loud (non-fatal) warning — a single
  bench/dev unit may legitimately run on `1`.

### Changed (commissioning robustness)

- **`atspi-bench` never strands a maintained relay.** The Force-Transfer /
  Inhibit verification asserted the relay, slept 2 s, then released — with no
  `try/finally`, so a Ctrl-C or dropped SSH during the hold could leave the ATS
  forced to the generator (or inhibited). The per-channel assert/hold/release
  is now wrapped in `try/finally`, and `_run()` has a run-exit safety net that
  drives Force Transfer + Inhibit OFF before closing the client (skipped when
  `--skip-dos`). The ADAM host watchdog above is the hardware layer beneath this.
- **`docs/HARDWARE.md §6` modpoll DI command fixed.** It told operators to read
  the DIs with bare `modpoll` (which reads holding registers, FC03) and `-c 1`
  (one register); corrected to `-t 1` (discrete inputs) / `-t 0` (coils) over
  the 6-channel range, tied to `io.adam.di_read`.

### Added (commissioning tooling)

- **`install.sh`** — one-shot Pi installer: creates the `atspi` service
  user/group, installs the package into a venv at `/opt/atspi/venv`, drops in
  the config (never clobbering an existing one) and the systemd unit (with
  `ExecStart` pointed at the venv). Idempotent; re-run after `git pull` to
  upgrade. Deliberately does NOT start the service — you bench-verify first.
- **`testadam.sh`** — bench-verify the ADAM-6060 before enabling the service:
  pings it, proves Modbus TCP works and prints a live snapshot of the 6 DIs +
  relays, then launches the interactive `atspi-bench` wiring walkthrough.
  Target host/port/unit default to `/etc/atspi/config.yaml`.

### Added (pre-hardware hardening)

- **Input debounce in the ADAM driver.** The five level inputs (on-normal,
  on-emergency, both source-available, engine-start) now pass through a
  per-channel integrator debounce: a change is published only after it holds
  for `io.adam.debounce_samples` consecutive 10 Hz samples (default 3 ≈
  300 ms). A single noisy read from contact bounce or EMI on a long
  control-wire run can no longer flip published state or drive the
  position/transfer-count logic. The momentary Load Disconnect contact (DI 0)
  is deliberately excluded — it's latched raw and stretched by
  `TRANSFERRING_HOLD_S`, so debouncing it would swallow the transfer edge.
  The first read seeds the baseline, so there's no startup delay. `1` disables.
- **`io.adam.assumed_mode` config.** The ADAM-6060 has no spare DI for an
  Auto/Manual sense contact, so ATS mode was hardcoded to `auto` — which made
  the ICD §6 mode policy inert (every command always permitted). Mode is now
  an explicit, operator-asserted config value (default `auto`, unchanged
  behaviour) that doubles as a command gate: `manual` lets only `cmd_inhibit`
  through, `test`/`unknown` block all commands. Setting `manual` makes the Pi
  observe-and-inhibit-only — GenWatch can never force a transfer or start a
  test through it. An invalid value fails fast at startup.

### Changed (operability)

- The sampling loop no longer logs a full traceback every 100 ms during an
  ADAM/network outage (that flooded the journal at ~10 lines/s and buried
  other logs). It logs the first failure of a streak at WARNING, throttles
  repeats to one reminder every ~30 s, and logs `sampling recovered …` when
  reads succeed again. RUNBOOK §3(a) and HARDWARE §7 updated to match.
- Dependabot now ignores pymodbus minor/major bumps (`.github/dependabot.yml`).
  It was regenerating a weekly PR to widen the pin to `<3.14`, which would let
  pymodbus 3.13 install and break `server.py` (the `ModbusSlaveContext` →
  `ModbusDeviceContext` rename). 3.7.x patch updates are still allowed.
- The CI `audit` job now scans only the project's own dependency closure
  (`pymodbus`, `PyYAML` + transitives, read from `pyproject.toml`) instead of
  the whole environment. It was permanently red — `--strict` failed on the
  un-publishable editable `atspi` package and on CVEs in the CI runner's base
  tooling (pip/setuptools/wheel), none of which ship to the Pi. Now it's green
  in the clean case, so a red result is a real, actionable advisory in a
  dependency the service actually ships.

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
- Writes with an out-of-pattern VALUE to a defined command register
  (e.g. `cmd_inhibit=5`, `cmd_test=0`) are acknowledged on the wire and
  ignored, instead of returning the ICD §6-preferred exception 0x03.
  pymodbus 3.7's `validate()` hook receives only (function code, address,
  count) — never the written value — so there is no hook from which to
  reject by value; the write is dropped after acknowledgement
  (`RegisterStore.write_register` returns no intent → no relay action, no
  read-back change). The safety-relevant property (an out-of-pattern value
  never reaches a relay) holds and is pinned, together with the
  acknowledged-and-ignored wire behaviour, by
  `tests/test_icd_contract.py::test_invalid_value_writes_are_acknowledged_and_ignored`.
  This supersedes the older "Fixed" claim further down that invalid-value
  writes return a Modbus exception — verified current behaviour is
  acknowledged-and-ignored; only invalid-*address* writes get the exception.
  GenWatch's command path only ever writes `0x0000`/`0x0001`, so the
  deviation is unreachable from the production client.

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
