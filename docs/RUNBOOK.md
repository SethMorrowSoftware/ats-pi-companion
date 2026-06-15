# Operations runbook

Field-troubleshooting reference for an ATS-Pi deployed at a site. For
install instructions see [HARDWARE.md](./HARDWARE.md) and
[DEVELOPMENT.md](./DEVELOPMENT.md); for design rationale see
[SPEC.md](./SPEC.md). This document is the "what do I do at 2am when
GenWatch is paging me" reference.

The conventions in this document:

- `<ats-pi>` is the static IP of the ATS-Pi on the OT VLAN
- `<adam-ip>` is the static IP of the ADAM-6060
- `<genwatch>` is the static IP of the GenWatch Pi
- All Modbus addresses are PDU offsets (hex) per the ICD

---

## 1. First moves (any incident)

```bash
# 1. Confirm the service is running
systemctl status atspi

# 2. Tail the journal — the last 100 lines usually tell you everything
sudo journalctl -u atspi -n 100 --no-pager

# 3. Confirm Modbus reachability from outside the box
modpoll -m tcp -a 1 -r 1 -c 6 <ats-pi>

# 4. Read the fault summary directly
modpoll -m tcp -a 1 -r 6 -c 1 <ats-pi>     # PDU 0x0005 = fault_summary
```

> `modpoll` isn't in Raspbian's default package set. Install once with
> `sudo apt install modbus-cli`, or grab the static binary from
> proconx.com. Same package on the GenWatch Pi.

`fault_summary` bits decoded:

| Bit | Mask | Meaning | What to check |
|---|---|---|---|
| 0 | `0x01` | INPUT_FAULT | Last ADAM read failed, OR a Modbus write was rejected by mode policy (see §3) |
| 1 | `0x02` | OUTPUT_FAULT | Last command write to ADAM failed, OR commanded-vs-readback mismatch (stuck relay), OR the F1 hardware fail-safe is unverified (see §4c) |
| 2 | `0x04` | MODE_UNKNOWN | The driver reports `ats_mode=unknown` (only relevant once a mode-sensing contact is wired) |
| 3 | `0x08` | CALIBRATION | Impossible contact/position combo — both position-sense inputs asserted at once (welded/miswired aux, or Group 5 status bit). Check the position sense against the real switch |

---

## 2. "GenWatch can't see the ATS-Pi"

GenWatch will fall back to H-100-derived load source. The Live view should
annotate "(via gen telemetry)" rather than show ATS data directly.

### Step 1: Is the ATS-Pi service up?

```bash
systemctl status atspi
```

- `active (running)` → service is up. Continue to step 2.
- `failed` or `inactive` → check `journalctl -u atspi -n 200` for the
  crash reason. Common causes:
  - Config file syntax error / unknown key (since the H1 fix, ConfigError
    is logged with the offending key's dotted path)
  - `/etc/atspi/config.yaml` missing
  - ADAM unreachable at boot — service still starts and retries (look for
    "I/O driver failed to connect; will keep retrying" in the journal)

### Step 2: Is the Modbus port reachable from GenWatch?

From the GenWatch Pi:

```bash
nc -zv <ats-pi> 5020
```

- Connection refused → service is up but Modbus server crashed. Restart:
  `sudo systemctl restart atspi`. With C5 in place, a crashed server task
  now triggers a process exit + systemd auto-restart, so this should
  self-heal — investigate if it doesn't.
- No route to host → networking / VLAN problem, not application.

### Step 3: Does the ATS-Pi reach the ADAM?

From the ATS-Pi:

```bash
ping -c 3 <adam-ip>
modpoll -m tcp -a 1 -r 1 -c 6 <adam-ip>
```

- ADAM unreachable → check power on the DR-30-24 PSU (green LED), cable,
  switch port LEDs. The ATS-Pi will keep retrying every 100 ms; recovery
  is automatic the moment the ADAM responds.

Under `driver: hybrid`, monitoring rides the **serial** link to the ASCO
Group 5 controller, not the ADAM — so also check the serial path:

```bash
ls -l /dev/ttyUSB*     # adapter present? (config default is /dev/ttyUSB0)
```

If the node is missing, the USB-RS485 adapter dropped off (re-seat it;
`dmesg | grep -iE 'ttyUSB|ttyACM'`). Confirm the controller's RS485
**address and baud** match `io.asco_serial.unit_id` / `baudrate` — a
mismatch reads as a serial timeout (INPUT_FAULT, §3), not a missing node.
The ADAM reachability check above still applies to the **control** (relay)
side in hybrid.

### Step 4: Is GenWatch's expected_unit_id matching?

The site identifier register is at PDU `0x0035`:

```bash
modpoll -m tcp -a 1 -r 54 -c 1 <ats-pi>   # 1-based, so 0x0035 + 1 = 54
```

This must match the `ats.expected_unit_id` line in GenWatch's config.
Deploy default is **23** (SITE-23). A mismatch silently drops the ATS-Pi
to non-authoritative in GenWatch — visible only as a `(via gen telemetry)`
annotation in the Live view, not as an error.

### Step 5: Are the DI values plausible? (wrong `di_read`)

Reads succeeding but `position` stuck at `unknown` and both
`*_available` registers reading `0` regardless of the ATS state is the
signature of the **wrong DI Modbus function code**. No `INPUT_FAULT` is
raised (the read itself succeeds; the bits are just always 0).

```bash
modpoll -m tcp -a 1 -r 1 -c 6 <ats-pi>     # 0x0000-0x0005 core state
```

→ Switch `io.adam.di_read` between `coils` (FC01) and `discrete_inputs`
(FC02) and restart. Confirm at the source with
`testadam.sh --di-read discrete_inputs` (HARDWARE.md §3). On the
ADAM-6000 series the documented DI mapping is FC02.

> **This step is `driver: adam` only.** In hybrid the DIs aren't read at
> all, so `di_read` is irrelevant — flipping it fixes nothing. The hybrid
> analogue of "reads succeed but the state is wrong" is a **wrong
> `io.asco_serial` status_register / bit map**: position/availability come
> from the serial holding register, so a mis-set `status_register` or a
> swapped `on_normal_bit` / `normal_available_bit` etc. yields a plausible-
> but-wrong served state with no INPUT_FAULT. Fix it by **bench-verifying the
> map against ASCO doc 381339-221** ([HARDWARE.md §3.1](./HARDWARE.md)) — read
> the register over RTU while operating the switch and confirm which bit
> tracks which fact — **not** by touching `di_read`.

---

## 3. "fault_summary shows INPUT_FAULT (bit 0)"

Two distinct causes share this bit:

### (a) The ADAM is unreachable or returning errors

Look for these in the journal:

```
sampling cycle failed (OSError): ADAM read_coils(0, 6) failed: ...
sampling still failing after 300 cycles (~30s): ADAM read_coils(0, 6) failed: ...
sampling recovered after 12 failed cycle(s) (~1s)
```

The sampling loop logs the **first** failure of a streak at WARNING, then
throttles repeats to one reminder every ~30 s (so a hard outage doesn't
flood the journal at 10 lines/s), and logs `sampling recovered …` when
reads succeed again. INPUT_FAULT clears on that first successful read.

> **Under `driver: hybrid` an INPUT_FAULT is a *serial* read failure, not an
> ADAM coil read.** Monitoring comes from the ASCO Group 5 controller over
> RS-485, so the journal shows `ASCO ... read_holding_registers ... failed`
> or `ASCO serial open ... failed` rather than `ADAM read_coils(...)`. Don't
> chase the ADAM here — diagnose the serial side: confirm the adapter node
> with `ls -l /dev/ttyUSB*`, that the controller's RS485 address/baud match
> `io.asco_serial`, and the wiring/termination per
> [HARDWARE.md §3.1](./HARDWARE.md). (The ADAM is still the **control** path
> in hybrid, so OUTPUT-side faults in §4 still point at the ADAM.)

**Is it a transient network blip or a real wiring fault?**
The driver uses a 500 ms Modbus timeout (1 retry). A single
`sampling cycle failed` line immediately followed by `sampling recovered`
is a normal transient. Worry only if:

- You see the `sampling still failing after N cycles` reminders keep
  coming (the outage is sustained) — likely cable, switch, or ADAM power
- `ping <adam-ip>` from the Pi fails — definitely network/cable
- `ping` works but every Modbus read times out — ADAM is locked up,
  power-cycle it
- Errors started AFTER weeks of stability — suspect the cable or
  switch port first (corrosion, bent pin) before suspecting the ADAM

### (b) A Modbus write was rejected by mode policy

Look for:

```
write to 0x010X rejected: ats_mode=manual, allowed=['auto']
```

→ A client (GenWatch admin write, modpoll test) tried to issue a command
that isn't permitted in the current ATS mode. The bit clears on the next
valid command. ICD §6 mode policy:

| Command | Allowed modes |
|---|---|
| `cmd_test` (0x0100) | auto only |
| `cmd_inhibit` (0x0101) | auto, manual |
| `cmd_force_transfer` (0x0102) | auto only |
| `cmd_bypass_delay` (0x0103) | auto only |

---

## 4. "fault_summary shows OUTPUT_FAULT (bit 1)"

Three distinct causes share this bit:

### (a) `drive_outputs()` to the ADAM failed

Look for:

```
command dispatch failed (intent=CommandIntent(...)): ...
ADAM write_coil(...) failed: ...
```

→ Network or ADAM-side problem. Diagnose like §2 step 3. The bit clears
on the next successful `drive_outputs()` call.

### (b) Stuck-relay detection fired

Look for:

```
ADAM DOn read-back mismatch: commanded=True actual=False (X.Xs since command)
  — possible stuck relay
```

→ The B1 detection observed a persistent mismatch between what we
commanded and what the ADAM read back, past the 500 ms settling window.
This usually means:

1. **Relay failure** — the ADAM's relay didn't actuate. Power-cycle the
   ADAM. If repeatable on a single DO, the relay coil or contact is
   damaged; replace the ADAM.
2. **Wiring fault** — a DO terminal is shorted, open, or backfeeding.
   Check field wires; verify with `atspi-bench --skip-dis --host <adam-ip>`.
3. **ADAM firmware quirk** — some firmwares debounce DO read-back at
   200 ms; if `OUTPUT_SETTLING_S` in `io_adam.py` is too tight for your
   firmware, bump it.

### (c) Hardware fail-safe unverified (F1) — commands also refused

Look for, at startup or after a reconnect:

```
ADAM host-watchdog fail-safe NOT verified — <reason>
ADAM hardware fail-safe NOT verified — ... Outputs will be REFUSED ...
```

and, if a command was attempted:

```
command dispatch failed (intent=...): refusing to assert ATS outputs —
  ADAM host-watchdog fail-safe not verified: <reason>
```

→ The driver could not confirm the ADAM's host-watchdog / DO safety-value
fail-safe is armed (`HARDWARE.md §5.1`, §5.2), so it is **refusing to assert
outputs** and holding OUTPUT_FAULT until that's resolved. The `<reason>` says
which: `not configured` (fill in `io.adam.hw_watchdog.*` register addresses
from the ADAM-6000 manual and bench-verify), `not enabled` / `timeout … out of
band` / `safety value(s) not OFF` (re-run the §5.1 watchdog setup in the
Advantech utility), or `could not read …` (ADAM unreachable — diagnose like
§2). De-asserts still work, so the safety watchdog can still drop relays. For
intentional bench work without the fail-safe, set
`io.adam.require_hw_watchdog: false`.

---

## 5. "GenWatch says ATS-Pi is unhealthy / comms lost"

GenWatch declares the ATS-Pi `lost` after 3 consecutive prime-poll
failures (≈4.5 s without a successful read). When it does, it falls
back to H-100-derived load source automatically.

From the ATS-Pi side, the safety watchdog ALSO sees this: after 30 s of
no successful Modbus read from any client, it auto-releases maintained
commands (cmd_inhibit, cmd_force_transfer) and physically drives those
relays off. Look for:

```
Modbus comms silent for XX.Xs (> 30.0s) — auto-releasing maintained commands per ICD §8.3
```

This is **expected, designed-in behavior** — not a bug. If you see it
in the journal during normal operation it means GenWatch stopped polling
for over half a minute. Investigate the GenWatch side first; the ATS-Pi
did exactly what the contract requires.

When GenWatch reconnects, the watchdog re-arms automatically; the next
Modbus read clears the `released` flag.

Related: a clean `systemctl stop` / `restart` also releases the command
outputs on the way down, and every service start re-releases them before
the first sample (ICD §9.3 reset-on-reboot) — so a restart never carries
a latched relay across boots, even when it completes faster than the
ADAM host-idle watchdog would have fired. Look for
`startup: ATS command outputs reset to released` /
`shutdown: ATS command outputs released` in the journal.

This software watchdog only covers *"GenWatch silent, Pi alive."* If the
**Pi itself** dies (power loss, panic) with a maintained command
asserted, only the **ADAM-6060's own host watchdog / DO fail-safe**
(HARDWARE.md §5.1) can release the relay. If you find the ATS stuck in
forced-transfer or inhibit after a Pi power event, check that the ADAM
host watchdog is actually enabled — verify with `atspi-bench --skip-dis`
(assert DO 2, pull the Pi's network cable, watch the relay drop).

---

## 6. "The service keeps restarting"

`systemctl status atspi` shows repeated activations.

```bash
sudo journalctl -u atspi -n 300 --no-pager | grep -E "(starting|shutting down|died)"
```

- "shutting down due to failure of critical task: <name>" → one of
  `sampling`, `safety-watchdog`, `modbus-server` died. The task name and
  exception trace are in the journal above the shutdown line. (C5
  guarantees this surfaces — pre-C5 the service would have hung silently
  instead.)
- Exit code is 1 on failure-path shutdown; systemd's `Restart=on-failure`
  retries every 5 s (`RestartSec=5`). If the underlying issue isn't
  fixed, you'll see a restart loop.
- If the loop is tight (many restarts/minute), systemd will eventually
  rate-limit. Run `sudo systemctl reset-failed atspi` after fixing the
  root cause to clear the rate-limit lockout.

---

## 7. "state.json is corrupt"

The persistence module tolerates this — it falls back to zeros and
logs a warning:

```
persisted state at /var/lib/atspi/state.json is unreadable (...); falling back to zeros
```

→ `transfer_count_lifetime` resets to 0. No data corruption beyond that.
The next transfer will write a fresh valid state.json.

If you want to manually inspect or repair:

```bash
sudo -u atspi cat /var/lib/atspi/state.json
# expected: {"transfer_count_lifetime": N, "last_transfer_to_gen_ts": ...,
#            "last_retransfer_to_util_ts": ...}
```

To reset the lifetime counter to a known value (e.g. after migrating
from a previous monitoring system):

```bash
sudo systemctl stop atspi
sudo -u atspi tee /var/lib/atspi/state.json <<EOF
{
  "transfer_count_lifetime": 1234,
  "last_transfer_to_gen_ts": 0,
  "last_retransfer_to_util_ts": 0
}
EOF
sudo systemctl start atspi
```

---

## 8. "ICD version mismatch alarm in GenWatch"

GenWatch reads `icd_version_major` (PDU `0x0030`) at startup. It refuses
to mark the ATS-Pi authoritative if it doesn't match `EXPECTED_ICD_MAJOR=1`.

```bash
modpoll -m tcp -a 1 -r 49 -c 2 <ats-pi>   # 0x0030, 0x0031
```

Expected: `[1, 0]` (major=1, minor=0).

- If you see `[2, ...]` → the ATS-Pi is on a newer ICD major that GenWatch
  hasn't caught up to. Roll back the ATS-Pi or update GenWatch.
- If the bytes don't decode at all → not an ATS-Pi on that port. Check
  what's actually listening: `sudo lsof -i :5020` on the box.

---

## 9. "Time skew warning"

GenWatch reads `ats_pi_wallclock` (PDU `0x0016`) on every base poll
(15 s) and compares against its own clock. Skew > 5 s logs
`ATS_PI_TIME_SKEW`.

```bash
# On both Pis, check chrony / NTP status
chronyc tracking
timedatectl
```

Both Pis must sync against the same source. If one has lost NTP and is
drifting, the warning is informational; correlations between H-100 events
and ATS-Pi events may be misaligned until the clock recovers.

---

## 10. Useful Modbus snippets

```bash
# Full state read (core + readback) — what GenWatch's prime poll fetches
modpoll -m tcp -a 1 -r 1 -c 6 <ats-pi>      # 0x0000-0x0005 core state
modpoll -m tcp -a 1 -r 65 -c 4 <ats-pi>     # 0x0040-0x0043 command read-back

# Full base poll equivalent
modpoll -m tcp -a 1 -r 17 -c 8 <ats-pi>     # 0x0010-0x0017 timestamps
modpoll -m tcp -a 1 -r 33 -c 4 <ats-pi>     # 0x0020-0x0023 counters
modpoll -m tcp -a 1 -r 49 -c 6 <ats-pi>     # 0x0030-0x0035 id/version

# Assert inhibit manually (ICD §6: cmd_inhibit allowed in auto+manual)
modpoll -m tcp -a 1 -r 258 -c 1 -t 4 <ats-pi> -- 1   # 0x0101 = 1 (assert)
modpoll -m tcp -a 1 -r 258 -c 1 -t 4 <ats-pi> -- 0   # 0x0101 = 0 (release)

# Trigger a Test pulse (auto mode only)
modpoll -m tcp -a 1 -r 257 -c 1 -t 4 <ats-pi> -- 1   # 0x0100 = 1
```

Reminder: `modpoll`'s `-r` is 1-based. PDU `0x0005` is `-r 6`,
PDU `0x0100` is `-r 257`.

---

## 11. Last resorts

### Roll back the service

```bash
sudo systemctl stop atspi
cd /path/to/ats-pi-companion   # the repo checkout you installed from
git fetch
git checkout <known-good-tag>
sudo ./install.sh              # idempotent; re-points the venv at this checkout
sudo systemctl start atspi
```

### Disable the ATS-Pi entirely (GenWatch falls back to H-100)

```bash
sudo systemctl disable --now atspi
```

GenWatch will detect the comms loss within 4.5 s and downgrade
authority to H-100. No restart needed on the GenWatch side.

### Wipe state and start fresh

```bash
sudo systemctl stop atspi
sudo rm -f /var/lib/atspi/state.json
sudo systemctl start atspi
```

Loses `transfer_count_lifetime`. The 24h sliding-window counter resets
anyway on every restart (by design — ICD §9.3 allows it).
