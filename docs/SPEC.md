# ATS-Pi Implementation Specification

| | |
|---|---|
| **Audience** | Engineer implementing the ATS-Pi server |
| **Pre-reads** | [ICD](https://github.com/SethMorrowSoftware/GenWatch/blob/main/docs/integrations/ats-pi-icd.md) (mandatory), [HARDWARE.md](./HARDWARE.md), [DEVELOPMENT.md](./DEVELOPMENT.md) |
| **Status** | Implemented; awaiting ADAM-6060 bench verification (Phase E) |

This document describes **how** to implement the ATS-Pi to honor the
ICD. The ICD specifies the wire-level contract — what GenWatch sees.
This document specifies what the ATS-Pi does internally to produce that
contract.

---

## 1. Software architecture

```
                  ┌─────────────────────────────────────┐
                  │              atspi                  │
                  │                                     │
                  │  ┌────────────────┐                 │
                  │  │ Modbus TCP     │ ◄── GenWatch    │
                  │  │ server         │                 │
                  │  │ (pymodbus)     │                 │
                  │  └───────┬────────┘                 │
                  │          │                          │
                  │  ┌───────▼────────┐                 │
                  │  │ Register store │                 │
                  │  │ (atspi.state)  │                 │
                  │  └───────┬────────┘                 │
                  │          ▲                          │
                  │  ┌───────┴────────┐                 │
                  │  │ Sampling loop  │                 │
                  │  │ (10 Hz)        │                 │
                  │  └───────┬────────┘                 │
                  │          │                          │
                  │  ┌───────▼────────┐                 │
                  │  │ I/O driver     │ ◄── hardware    │
                  │  │ (abstract)     │                 │
                  │  └────────────────┘                 │
                  │     ▲           ▲                   │
                  │  io_mock     io_adam                │
                  └─────────────────────────────────────┘
```

Five concerns, cleanly separated:

1. **I/O driver** — physical contact reads + relay drives. Abstract base
   class with two concrete impls: `io_mock` (for dev/CI) and `io_adam`
   (for production, Advantech ADAM-6060 over Modbus TCP).
2. **Register store** — in-memory representation of the ICD §5 register
   layout. Single source of truth that the sampling loop writes and
   the Modbus server reads. Atomic snapshot publication so torn reads
   are impossible (ICD §8.1).
3. **Sampling loop** — reads the I/O driver at 10 Hz, applies contact
   debouncing, updates the register store. Emits no events itself — it
   only updates state.
4. **Modbus TCP server** — pymodbus server bound to the register store.
   Translates client reads/writes into store operations. Stateless.
5. **Safety watchdog** — monitors time-since-last-successful-read from
   GenWatch. On the 30 s comms-loss threshold (ICD §8.3) auto-releases
   any asserted maintained commands by writing 0 to their registers.

---

## 2. I/O driver interface

`atspi.io_driver.IODriver` is an abstract base class. All physical I/O
must go through it. The two implementations:

| Class | Use case | Notes |
|---|---|---|
| `IOMockDriver` | Dev, CI, manual integration testing | Holds in-memory contact states; flippable from a CLI/REPL |
| `IOAdamDriver` | Production | Talks to ADAM-6060 over Modbus TCP using pymodbus client |

```python
class IODriver(Protocol):
    # Read all six inputs in a single atomic operation. Returns
    # (normal_avail, emerg_avail, pos_normal, pos_emerg, engine_start,
    # load_disconnect_pulsed).
    async def read_inputs(self) -> InputSnapshot: ...

    # Set the four maintained-or-pulsed outputs. Each is True (assert)
    # or False (release). Pulsed outputs (test, bypass) are released
    # automatically by the driver after the duration passed.
    async def drive_outputs(
        self,
        test_pulse_ms: int | None = None,
        inhibit: bool = False,
        force_transfer: bool = False,
        bypass_delay_pulse_ms: int | None = None,
    ) -> None: ...

    # Read back the actual driven state, for ICD §5.5 mirror.
    async def read_output_state(self) -> OutputState: ...

    # Lifecycle
    async def connect(self) -> bool: ...
    async def close(self) -> None: ...
```

The mock implementation backs each method with a `dict` and exposes
public setters so tests can flip inputs and observe what the server
publishes. The ADAM-6060 implementation wraps a pymodbus client and
maps the abstract operations to the ADAM's documented register map.

---

## 3. Sampling loop

```python
async def sampling_loop(driver: IODriver, store: RegisterStore):
    while True:
        try:
            snapshot = await driver.read_inputs()
            store.apply_input_snapshot(snapshot)
            output_state = await driver.read_output_state()
            store.apply_output_state(output_state)
        except Exception as e:
            log.exception("sampling cycle failed: %s", e)
            store.set_fault_bit(OUTPUT_FAULT)
        await asyncio.sleep(0.1)  # 10 Hz
```

Key requirements:

- **One atomic store update per cycle** — `apply_input_snapshot` must
  swap all five core-state values atomically so a Modbus read can never
  see a mid-update split state (ICD §8.1).
- **No exceptions escape** — a driver fault bumps a fault bit in the
  register store and the loop continues. We never crash the service
  over a transient I/O hiccup.
- **Contact debounce** — the ADAM driver runs a per-channel integrator
  debounce on the five *level* inputs (on-normal, on-emergency, both
  source-available, engine-start): a change is published only after it
  holds for `io.adam.debounce_samples` consecutive 10 Hz samples
  (default 3 ≈ 300 ms), so a single noisy read can't flip published
  state or spuriously drive the position/transfer-count logic. The
  *momentary* Load Disconnect contact (DI 0) is deliberately excluded —
  it's latched on the raw read and stretched by `TRANSFERRING_HOLD_S`, so
  debouncing it would swallow the edge. The first read seeds the baseline
  so there's no startup delay. See `io_adam._Debouncer`.

---

## 4. Modbus TCP server

Use `pymodbus.server.StartAsyncTcpServer`. The data block is a custom
subclass of `ModbusSequentialDataBlock` that translates `getValues` /
`setValues` calls into reads/writes on the `RegisterStore`:

```python
class LiveDataBlock(ModbusSequentialDataBlock):
    def __init__(self, store: RegisterStore):
        super().__init__(0, [0] * 0x0200)
        self._store = store

    def getValues(self, address, count=1):
        # pymodbus passes 1-based addresses
        return [self._store.read_register(address - 1 + i) for i in range(count)]

    def setValues(self, address, values):
        for i, v in enumerate(values):
            self._store.write_register(address - 1 + i, int(v))
```

Bind to port 5020 (configurable; the privileged 502 needs root the service
doesn't have). Unit ID 1. No authentication (ICD §3).

---

## 5. Safety watchdog (ICD §8.3)

This is the **single most safety-critical component** of the service.
If GenWatch goes silent (network failure, browser close, kernel
panic, anything) while a maintained command is asserted, the ATS-Pi
must release that command within 30 s.

```python
class SafetyWatchdog:
    """Watches time-since-last-read from any Modbus client.
    Auto-releases maintained commands after timeout.
    """
    TIMEOUT_S = 30

    def __init__(self, store: RegisterStore):
        self._store = store
        self._last_read_monotonic = time.monotonic()
        self._released_for_timeout = False

    def note_modbus_read(self) -> None:
        """Called by the data block on every successful read."""
        self._last_read_monotonic = time.monotonic()
        self._released_for_timeout = False

    async def run(self):
        while True:
            await asyncio.sleep(1.0)
            elapsed = time.monotonic() - self._last_read_monotonic
            if elapsed > self.TIMEOUT_S and not self._released_for_timeout:
                log.warning(
                    "comms timeout (%.1fs since last read) — auto-releasing maintained commands",
                    elapsed,
                )
                self._store.release_maintained_commands()
                self._released_for_timeout = True
```

The exact algorithm:

1. Hook `note_modbus_read()` into `LiveDataBlock.getValues` so every
   successful read updates the last-read time.
2. The watchdog task wakes every second and checks elapsed time.
3. On timeout: set `cmd_inhibit_active` and `cmd_force_transfer_active`
   to false, and physically release the corresponding outputs via the
   I/O driver. Pulsed commands (`test`, `bypass_delay`) self-clear
   already so they don't need handling here.
4. The auto-released state is reflected in the next Modbus read (read-
   back registers at `0x0040-0x0043`), so a recovering GenWatch
   correctly observes "I had inhibit on, but it's been released."
5. Once a successful read happens again, the released-for-timeout
   flag clears and the watchdog re-arms.

**Test this with a real timer.** A unit test that mocks `time.monotonic`
is not sufficient — the safety guarantee must hold against real wall
time.

**This watchdog only protects against host-alive comms loss.** It runs
*inside* this service, so it cannot fire if the Pi itself dies (power
loss, kernel panic, unplugged) while a maintained command is asserted —
the ADAM would latch the relay forever. The hardware backstop for that
case is the **ADAM-6060's own host-idle watchdog + DO safety values**,
configured at commissioning (`docs/HARDWARE.md §5.1`) with all DOs
de-energising on host loss. The two layers are complementary: this one
handles "GenWatch silent, Pi alive" in 30 s; the ADAM watchdog handles
"Pi dead" in 5–10 s.

Because that hardware backstop is the single safety-critical item and is
configured by hand, the driver **reads it back on connect and refuses to
drive the switch unless it confirms the fail-safe is armed** (F1) —
`io.adam.require_hw_watchdog`, default on; details in `docs/HARDWARE.md §5.2`.
A failed/unverifiable check fails closed: outputs are refused (releases still
allowed) and a persistent `OUTPUT_FAULT` is published. The refusal that blocks
the relay is enforced *locally* by this driver; GenWatch surfaces the
`OUTPUT_FAULT` as a fault alarm (it does not gate command authority on it).

---

## 6. State persistence

Per ICD §9.3:

- **MUST persist:** `transfer_count_lifetime`. Survives reboots.
- **SHOULD persist:** `last_transfer_to_gen_ts`, `last_retransfer_to_util_ts`.
- **MUST reset on reboot:** `ats_pi_uptime_s` (per definition), all
  command registers (start in "no commands asserted" state per §9.3).

  The command reset is enforced *actively*, not just in the store: the
  sampling loop's first action is `driver.release_all_outputs()` — all
  four command DOs driven OFF, retried each cycle until the write lands.
  Without that, a service restart fast enough to beat the ADAM's
  host-idle watchdog (a `systemctl restart` completes in ~2 s; the
  watchdog needs 5–10 s of silence) would carry a relay latched by the
  previous instance into the new boot with nothing left to release it.
  The same release runs once more on graceful shutdown, so a stopped
  service never leaves a relay latched waiting for the hardware
  fail-safe (or latched indefinitely where that fail-safe isn't
  configured, e.g. mid-bench).

Implementation: JSON file at `/var/lib/atspi/state.json` written on
each transition. Atomic-rename + parent-directory fsync so a power
loss either keeps the old file or shows the new one in full, never
half-written. Read at boot, default to zeros on missing or corrupt.

We persist one field beyond what the ICD requires: the 24-hour
sliding window's individual transfer timestamps. ICD §1.3 says
`transfer_count_24h` MAY reset on reboot, but persisting the window
removes an ops papercut — an unrelated service restart no longer
zeroes the daily count mid-day. Timestamps older than the 24 h cutoff
are evicted at load time, so a long outage doesn't carry stale
entries forward.

See `persistence.py` and `test_persistence.py` for the atomic-write
machinery and corruption-tolerance tests.

---

## 7. Configuration

YAML, loaded from `--config` flag. Example:

```yaml
modbus_server:
  host: 0.0.0.0          # 0.0.0.0 = listen on all interfaces
  port: 5020             # high port: the non-root service can't bind 502
  unit_id: 1

io:
  driver: adam            # 'adam' or 'mock'
  adam:
    host: 192.168.1.251   # ADAM-6060's IP
    port: 502
    unit_id: 1
    debounce_samples: 3   # consecutive samples a level input must hold (1=off)
    assumed_mode: auto    # no Auto/Manual contact; also gates commands (ICD §6)
    di_read: coils        # DI function code: coils (FC01) | discrete_inputs (FC02)

site:
  unit_id: 23             # ats_pi_unit_id register (ICD §5.4)

persistence:
  state_file: /var/lib/atspi/state.json

# Optional: localhost JSON health endpoint. Off by default.
# health:
#   enabled: true
#   host: 127.0.0.1
#   port: 8001
```

Log level is set via the CLI (`atspi --log-level DEBUG`), not the
config file — production deployments rely on systemd-journal capture
and `journalctl -u atspi -p info`. The strict config loader rejects
unknown keys, so adding `logging:` to the YAML will fail-fast.

Validate at startup; fail fast on missing required fields.

---

## 8. Implementation phasing

Suggested work breakdown (the GenWatch side's Phase 1 is already done,
so this is purely the ATS-Pi side):

| Phase | What | Status | Test |
|---|---|---|---|
| **A** | Register store + mock I/O driver + Modbus server | ✅ done | `modpoll` reads correct values; flipping mock inputs changes responses |
| **B** | Sampling loop + atomic snapshot | ✅ done | `_StateSnapshot` is frozen and swap-published; covered by `test_state.py` |
| **C** | Write command handling | ✅ done | Writes return a `CommandIntent`, dispatched to the driver via `on_command`; read-back updates from the next sampling cycle |
| **D** | Safety watchdog | ✅ done | `test_safety.py` covers timeout / re-arm / driver-error swallowing |
| **E** | ADAM-6060 I/O driver | ⚠️ code complete, awaiting bench verification | `test_io_adam.py` covers logic against a fake client; bench-verify steps in `io_adam.py` docstring |
| **F** | Persistence | ✅ done | `persistence.py` + `test_persistence.py`; round-trip across restart in `test_state.py::test_transfer_count_persists_across_restarts` |
| **G** | Production install — systemd, config, real ASCO wiring | pending | Golden test sequence (ICD §13) passes against real hardware |

Phases A-D and F are done entirely with mock I/O. Phase E's code is
written but requires the ADAM-6060 on the bench to confirm the coil
address map matches your firmware revision (see `io_adam.py` docstring
for the verification checklist).

---

## 9. Acceptance criteria

The implementation is complete when:

1. The ICD §13 golden test sequence passes against the real ATS-Pi
   wired to the real ASCO.
2. GenWatch's commissioning checklist
   ([`ats-pi-plan.md`](https://github.com/SethMorrowSoftware/GenWatch/blob/main/docs/integrations/ats-pi-plan.md)
   §8) clears with `ats.enabled: true`.
3. The safety watchdog releases maintained commands within 30 ± 5 s of
   GenWatch comms going silent — verified by stopping GenWatch with
   inhibit asserted and observing the release.
4. ICD version match: `icd_version_major` register reads `1`,
   `icd_version_minor` reads `0` (or higher minor for additive changes).
5. The systemd service starts automatically on boot and recovers
   automatically on crash (no manual intervention needed).

---

## 10. Resolved design decisions

The original draft of this spec carried an "open questions" section.
Each has since been resolved; recording the outcomes here so future
readers know the rationale.

- **Persistence format.** JSON file with atomic-rename + parent-dir
  fsync. Falls back to zeros on missing/corrupt. SQLite was overkill
  for a four-field record. See `persistence.py`.
- **Watchdog timeout precision.** 1 s polling against
  `time.monotonic()`. Real auto-release fires in `[30, 31] s`, well
  inside ICD §8.3's `30 ± 5 s` window. The watchdog retries
  `drive_outputs` every tick on failure so a transient ADAM blip
  during the release event can't strand a maintained command.
- **Output relay readback.** Implemented as commanded-vs-actual
  comparison with a 500 ms settling window after each write
  (`io_adam.check_output_consistency`). Mismatches outside the
  window set `OUTPUT_FAULT` per ICD §5.1.1.
- **Logging.** systemd-journal-friendly stdout, no rotation.
  Log level via `--log-level` CLI flag — config-file log level was
  considered and rejected (one fewer thing to keep in sync, and the
  strict config loader would reject an unknown `logging:` section
  anyway).
