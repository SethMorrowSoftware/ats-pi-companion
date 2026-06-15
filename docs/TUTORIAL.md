# Tutorial: from parts on the bench to live in GenWatch

A single, linear, follow-along walkthrough of the whole job — power up the
ADAM, prove it on the bench, wire it to the **ASCO Series 300**, and get the
state showing up in **GenWatch**. It is an orchestration layer over the three
reference docs; each phase points to the canonical doc for the deep detail and
rationale. Do the phases **in order**.

```
┌─────────────┐   dry      ┌─────────────┐   Modbus    ┌─────────────┐   Modbus    ┌──────────┐
│  ASCO 300   │  contacts  │  ADAM-6060  │    TCP      │   ATS-Pi    │    TCP      │ GenWatch │
│  ATS        │ ◀────────▶ │  6 DI + 6   │ ◀─────────▶ │   service   │ ◀─────────▶ │  Pi      │
│             │            │  relay DO   │             │ (this proj) │  port 5020  │ dashboard│
└─────────────┘            └─────────────┘             └─────────────┘             └──────────┘
   Phase 6           Phases 1–2                    Phases 1,3                  Phase 4
```

## The golden rule

> **Everything that can fail at a desk fails at a desk, not at 480 V.**

This service *commands* a live transfer switch. You prove the entire chain —
ADAM ↔ ATS-Pi ↔ GenWatch — and the hardware fail-safe **on the bench** first,
so the only thing left at the cabinet is landing wires. Do **not** land a
single wire on the ASCO until Phases 1–5 are signed off. The ATS internals are
at 480 V / 600 A; all cabinet work requires a qualified electrician with LOTO
and PPE per NFPA 70E (`HARDWARE.md §8`).

## What you'll do

| Phase | Goal | Canonical doc | Gate |
|---|---|---|---|
| 0 | Gather parts, survey the ASCO, pick IPs | `HARDWARE.md §1–2` | — |
| 1 | Bench-prove the ADAM (reads, relays, round-trip, watchdog) | `BENCH.md` | bench checklist |
| 2 | Configure + **cable-pull verify** the hardware fail-safe | `NEXTSTEPS.md §1–2` | **F1** |
| 3 | Production config + lock the network down | `NEXTSTEPS.md §3` | **F3** |
| 4 | **Talk to GenWatch** (still on the bench) | `NEXTSTEPS.md §4` | ICD, §8.3 |
| 5 | Tag the build, fill the sign-off sheet | `NEXTSTEPS.md §5` | **F4** |
| 6 | Wire it to the ASCO + final commissioning | `HARDWARE.md §3,5,7` | go-live |

Phases 1–5 happen at a desk with jumper wires. Phase 6 is the only one at the
cabinet.

---

## Phase 0 — Before you touch anything

1. **Confirm the bill of materials** (Pi 5, ADAM-6060, 24 VDC PSU, DIN rail,
   Cat6 + surge protectors, control wire). Full list with part numbers:
   `HARDWARE.md §1`.

2. **Survey the ASCO cabinet** (with LOTO). You need three accessories present,
   or the integration has nothing to read (`HARDWARE.md §2`):
   - **18RX REX module** (kit 935148) → source-availability contacts RL5/RL6
   - **14AA/14BA aux contact kit** → switch-position contacts
   - the **engine-start wire** to the H-100 — note it, **do not disturb it**

   If the 18RX or aux contacts are missing, order them and install during your
   next planned ATS outage *before* starting.

3. **Pick the addresses** (recommended defaults; both on the OT VLAN):
   - ADAM-6060: `192.168.1.251`
   - ATS-Pi: `192.168.1.250`
   - GenWatch Pi: its existing OT IP
   - `site.unit_id`: your real per-site id (the project's example is `23`).
     Write it down — GenWatch's `expected_unit_id` must equal it (Phase 4).

---

## Phase 1 — Bench bring-up (no ASCO, no GenWatch)

> Full procedure with the per-step gotchas: **[`BENCH.md`](./BENCH.md)**. This
> is the condensed path. The read scripts here stand in for GenWatch — it does
> **not** need to be running yet.

Direct-cable the Pi to the ADAM. The ADAM ships at `10.0.0.1/24`, Modbus TCP
on `502`.

> **Hybrid path:** this phase bench-verifies the **ADAM (control) half** below.
> You must **also** bring up the serial monitoring half — confirm the USB-RS485
> device node, wire A/B to the Group 5 controller, and bench-verify its
> register/bit map — per **[`HARDWARE.md §3.1`](./HARDWARE.md)**. The DI steps
> below (`di_read`, jumper each DI) are **contact-path-only**; in hybrid the ADAM
> DIs are unused and monitoring comes from the controller over serial.

```bash
# Pi: join the ADAM's factory subnet and confirm the link (BENCH.md §2)
sudo ip addr add 10.0.0.2/24 dev eth0
ping -c3 10.0.0.1

# Install the service in a dev venv (BENCH.md §3)
sudo apt update && sudo apt install -y python3-venv git
git clone https://github.com/SethMorrowSoftware/ats-pi-companion.git
cd ats-pi-companion
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
atspi --version
```

Now walk the hardware. `testadam.sh` pings, prints a live snapshot of all 6 DIs
+ 6 relays, then runs the interactive `atspi-bench` walkthrough:

```bash
./testadam.sh --host 10.0.0.1
```

- **Confirm `di_read`** (the one per-unit setting). On the ADAM-6060 the DIs
  answer on **coils** (FC01) — the default — confirmed on the bench and in the
  manual (App. B.2.8). If a future unit reads all-`0`/all-`1` no matter what you
  jumper, re-run with `--di-read discrete_inputs` and set `io.adam.di_read` to
  match. (`BENCH.md §5`, `HARDWARE.md §3`.)
- **DIs**: at each prompt, jumper that channel to its DI-common and press Enter;
  the tool confirms *exactly that channel* changed. DIs are dry-contact —
  **open reads `1`, shorted-to-common reads `0`**. Remove the jumper after each
  channel for a clean sweep. (`BENCH.md §6`.)
- **Relays**: the 6060 has **no per-channel I/O LEDs**, so confirm outputs by
  Modbus read-back + a meter, not lights (`BENCH.md §7`). Use the `relaytest.py`
  helper in `BENCH.md §7` to hold each relay long enough to meter it. Every
  relay must read `0` at rest, **including after a power-cycle**.

Then prove a physical contact flows all the way to a served ICD register, and
that the software comms-loss watchdog drops a maintained relay on its own:

- **Full round-trip** (`driver: adam`): `BENCH.md §8`. Jumper DI3 → the served
  `normal_available` register flips `1 → 0`.
- **Comms-loss auto-release** (ICD §8.3): `BENCH.md §9`. Assert Inhibit, go
  silent, watch DO2 drop at ~30 s.

> **Expected on a bare bench (contact path, `driver: adam`):** `position` reads
> **`transferring`**, not `unknown` — DI0 (Load Disconnect) floats high and reads
> as "transfer in progress." Not a fault. `fault_summary: 0` is what you want.
> (`BENCH.md §8`.) On the **hybrid** path there's no ADAM DI0, so this artifact
> doesn't occur — position comes from the Group 5 bits.

✅ **Gate — bench checklist** (`BENCH.md §12`): ping + snapshot, `di_read`
confirmed, all 6 DIs track their jumper, all 6 relays drive + read back + rest
at `0`, service round-trip works, comms-loss release fires at ~30 s.

---

## Phase 2 — Hardware fail-safe + cable-pull (F1) — **HARD GATE**

> Canonical: **[`NEXTSTEPS.md` Stage 1–2](./NEXTSTEPS.md)** and
> [`BENCH.md §10`](./BENCH.md).

This is the backstop for **"the Pi itself dies with a relay latched."** The
software watchdog from Phase 1 can't cover that — it dies with the process.
Only the ADAM's own **host-idle watchdog + per-DO Fail-Safe Value (FSV)** can
release a latched Force-Transfer/Inhibit relay when the Pi loses power.

1. **Configure it in the Adam/Apax .NET Utility (Windows).** The 6060's web UI
   is a dead Java applet, and the FSV/WDT are **not Modbus-exposed** on this
   model — the .NET Utility is the only path (`BENCH.md §10`, finding 4):
   - **Static IP** → the OT address you picked (`192.168.1.251`). Update
     `io.adam.host` to match.
   - **Host idle (timeout): 5–10 s** — longer than a sampling blip, **shorter
     than the 30 s software watchdog** so it only fires on true host loss.
   - **FSV: leave the box UN-checked for all six DOs** → each relay
     de-energises (0/OFF) on timeout, so a Pi crash *releases* commands instead
     of latching them. **Do not trust the checkbox label** — step 2 proves the
     direction. (`NEXTSTEPS.md §1.4`.)
   - **Power-on value OFF** for every DO.

2. **Cable-pull acceptance test (F1)** — the only test that proves the relay
   *physically* drops. Service stopped, watchdog configured:

   ```bash
   # Latch DO2 (Inhibit) directly on the ADAM — no master maintaining it
   python - <<'PY'
   import asyncio
   from pymodbus.client import AsyncModbusTcpClient
   async def m():
       c=AsyncModbusTcpClient(host='192.168.1.251',port=502,timeout=2); await c.connect()
       await c.write_coil(address=0x12, value=True, slave=1)   # 0x12 = DO2
       print('DO2 latched ON'); c.close()
   asyncio.run(m())
   PY
   ```

   Meter DO2 closed → **physically pull the Pi↔ADAM Ethernet cable** → within
   the host-idle timeout the relay must **drop** (you hear it click; the meter
   opens). Reconnect and confirm `0` (full read-back snippet:
   `NEXTSTEPS.md §2`). Because the Pi was disconnected, only the ADAM's own
   watchdog could have dropped it.

> **`io.adam.require_hw_watchdog` stays `false` on the 6060.** That software
> gate reads the watchdog config back over Modbus to confirm it's armed — but
> the 6060 doesn't expose those registers, so it would always fail closed. The
> protection here is **procedural**: the cable-pull record + the signed config
> are the safety control. **Re-run this test after any ADAM swap or factory
> reset** — nothing in software will warn you the FSV is gone.
> (`NEXTSTEPS.md §3.2`, `BENCH.md §10`.)

✅ **Gate F1**: latched relay physically drops within the timeout — **recorded
on the sign-off sheet** (host-idle timeout used, meter opened, post-pull read).

---

## Phase 3 — Production identity + network safety (F3)

> Canonical: **[`NEXTSTEPS.md` Stage 3](./NEXTSTEPS.md)**,
> [`HARDWARE.md §4.1`](./HARDWARE.md).

1. **Move to the production config.** `sudo ./install.sh` creates the service
   user, a venv at `/opt/atspi/venv`, the systemd unit, and
   `/etc/atspi/config.yaml` (idempotent; never clobbers an existing config; and
   it deliberately leaves the service **stopped**). Then edit
   `/etc/atspi/config.yaml`:

   ```yaml
   io:
     driver: adam                 # NOT mock — mock serves a fake healthy snapshot forever
     adam:
       host: 192.168.1.251        # the ADAM's OT IP
       di_read: coils             # confirmed in Phase 1
       require_hw_watchdog: false  # required on the 6060 (Phase 2)
   modbus_server:
     host: 192.168.1.250          # the Pi's OT IP — NOT 0.0.0.0 (see step 2)
     port: 5020
   site:
     unit_id: 23                  # your real id — MUST equal GenWatch expected_unit_id
   persistence:
     state_file: /var/lib/atspi/state.json   # real path, not /tmp
   ```

   > **Hybrid path:** set `io.driver: hybrid` and add the `io.asco_serial` block
   > (device, baud, controller address, and the bench-verified `status_register`
   > + bits) — `HARDWARE.md §3.1`. Without it the service **refuses to start**.
   > The `io.adam` block stays (it's the control side); the ADAM DIs are unused.

2. **Lock down the network (F3).** Modbus/TCP has **no authentication** —
   anything that can route to `:5020` can drive the switch within the mode
   policy, so the network boundary *is* a safety control. Bind to the OT IP
   (above) and allowlist only the GenWatch Pi:

   ```bash
   sudo iptables -A INPUT -p tcp --dport 5020 -s <genwatch-ip> -j ACCEPT
   sudo iptables -A INPUT -p tcp --dport 5020 -j DROP
   ```

   **Acceptance:** from a host *off* the OT segment, `nmap -p 5020 <ats-pi-ip>`
   shows the port **filtered**.

✅ **Gate F3**: bound to the OT IP, firewall allowlist in place, `nmap` shows
filtered from off-segment — **recorded**.

---

## Phase 4 — Get it communicating with GenWatch

> Canonical: **[`NEXTSTEPS.md` Stage 4](./NEXTSTEPS.md)**. This is the "talking
> to GenWatch" milestone — and you prove it on the bench, before the cabinet.

The ATS-Pi from Phase 3 is now serving real ADAM state on the OT network.

1. **Point GenWatch at the ATS-Pi.** In GenWatch's `/etc/genwatch/config.yaml`:

   ```yaml
   ats:
     enabled: true
     host: <ats-pi OT IP>      # 192.168.1.250
     port: 5020                # must match modbus_server.port
     expected_unit_id: 23      # MUST equal the ATS-Pi's site.unit_id
   ```

   Restart GenWatch. **A `expected_unit_id` mismatch makes GenWatch silently
   drop the link** (no error on the wire) — confirm the ids match first.

2. **Confirm the card populates.** GenWatch's **ATS card lights up within ~2
   prime polls (~3 s)** and the load source annotates **"(via ATS-Pi)"**. Spot-
   check the identity registers the ATS-Pi is serving (note `-a 1` is the Modbus
   slave id = `modbus_server.unit_id`; the value `23` is `site.unit_id`):

   ```bash
   modpoll -m tcp -a 1 -r 54 -c 1 <ats-pi OT IP>   # 0x0035 site.unit_id  -> 23
   modpoll -m tcp -a 1 -r 49 -c 2 <ats-pi OT IP>   # 0x0030/0x0031 ICD ver -> 1, 0
   ```

3. **Drive DI transitions, watch GenWatch follow.** Jumper DIs and confirm the
   GenWatch card tracks them — this proves the full chain visually (contact →
   ADAM → ATS-Pi → GenWatch). DIs are dry-contact (open = 1, jumpered = 0); the
   table in `NEXTSTEPS.md §4.3` lists the jumper combos to present "on utility",
   "on generator", "normal source lost", "engine-start calling".

   > **Hybrid path:** the ADAM DIs are unused — jumpering moves nothing. Drive
   > the **controller / switch** (or your Group 5 RTU simulator) instead and
   > watch the card follow (controller → serial → ATS-Pi → GenWatch).

   > On a bare bench the **contact path** shows **position = transferring** (DI0
   > floats high). Expected; the real position appears once the ASCO is wired.
   > (Hybrid reads position from the Group 5 bits — no DI0-float artifact.)

4. **End-to-end comms-loss release (ICD §8.3)** — driven from GenWatch this
   time, not a script: assert Inhibit from GenWatch → confirm DO2 energises →
   `sudo systemctl stop genwatch` → wait ~35 s → confirm the relay released and
   the read-back cleared:

   ```bash
   modpoll -m tcp -a 1 -r 66 -c 1 <ats-pi OT IP>   # 0x0041 cmd_inhibit read-back -> 0
   ```

✅ **Gates ICD + §8.3**: card populates, `expected_unit_id == site.unit_id`, DI
transitions track on the card, and an Inhibit asserted from GenWatch
auto-releases ~30 s after GenWatch goes silent.

---

## Phase 5 — Tag the build (F4)

> Canonical: **[`NEXTSTEPS.md` Stage 5](./NEXTSTEPS.md)**,
> [`HARDWARE.md §9`](./HARDWARE.md).

Never deploy a moving `main`. Before transporting to the cabinet:

- **Tag the exact commit** you commissioned against (e.g. `v0.1.0-rc1`) and
  deploy *only* that tag (`git checkout v0.1.0-rc1` before `install.sh`).
  `atspi --version` then matches the sign-off sheet.
- Commission against the **matching GenWatch tag**.
- **The sign-off sheet** records: the deployed tag + `atspi --version`, the F1
  cable-pull result, the ADAM config (static IP, host-idle time, all FSV
  unchecked / power-on OFF), the F3 `nmap` filtered check, and the Phase-4
  card-populate / unit_id / end-to-end release.

✅ **Go/no-go for cabinet install:** all five gates (F1, F3, F4, ICD, §8.3)
recorded as pass, with **F1 verified on the real unit**.

---

## Phase 6 — Wire it to the ASCO (the cabinet)

> Canonical: **[`HARDWARE.md §3, §5, §6, §7`](./HARDWARE.md)**. Only now go to
> the switch. What remains is physical.

> 🔧 **Hand the installer [`FIELD-INSTALL.md`](./FIELD-INSTALL.md)** — a
> one-page, print-and-follow wiring card (the §6.1 table as a checkbox per wire,
> plus power, network, and read-side-first verification) that needs no other doc
> at the cabinet. Record the result on [`SIGN-OFF.md`](./SIGN-OFF.md), and
> pre-stage [`config.production.example.yaml`](../config.production.example.yaml).

### 6.1 The terminal mapping

This is the wiring. Each row is one channel on the ADAM-6060.
**Canonical source: `HARDWARE.md §3`** — and **verify the ASCO terminal labels
against operator's manual `381333-289` for your specific catalog number**, pin
numbering varies by unit.

**Inputs — ADAM DIs read *from* the ATS:**

| ADAM DI | ASCO source | Reads |
|---|---|---|
| DI 0 | Load Disconnect contact (terminals 1↔2) | momentary pulse during transfer → `position=transferring` |
| DI 1 | Aux **14AA** NO contact | "On Normal" position |
| DI 2 | Aux **14BA** NO contact | "On Emergency" position |
| DI 3 | **18RX RL6** NO contact | Normal source available |
| DI 4 | **18RX RL5** NO contact | Emergency source available |
| DI 5 | Engine-start contact (sense in parallel with the H-100 wire) | ATS asserting engine-start |

**Outputs — ADAM relay DOs drive *into* the ATS:**

| ADAM DO | ASCO destination | Drives |
|---|---|---|
| DO 0 | Momentary Test Switch (terminals 6–7) | `cmd_test` — ≥ 500 ms pulse |
| DO 1 | Maintained Transfer (terminals 8–9) | `cmd_force_transfer` (maintained) |
| DO 2 | Inhibit Transfer (terminals 10–11) | `cmd_inhibit` (maintained) |
| DO 3 | Bypass Transfer Time Delay (terminals 12–13) | `cmd_bypass_delay` — ≥ 500 ms pulse |
| DO 4 / DO 5 | (spare) | — |

> ⚠️ **Never** wire DO 4 or DO 5 to ATS terminals 14, 15, or 16 — those are
> factory-use only and writing to them may damage the controller.

### 6.2 Install sequence (`HARDWARE.md §5`)

1. **LOTO** the ATS — utility **and** generator sources.
2. Mount the PSU, ADAM, and Pi enclosure on DIN rail in/adjacent to the cabinet.
3. Land 120 VAC L/N/G to the PSU; verify 24 VDC out.
4. Land **DI wires** from the ASCO contact terminals to the ADAM DI channels
   per §6.1.
5. Land **DO wires** from the ADAM relays to the ASCO input terminals per §6.1.
6. Wire Cat6 to both Pi and ADAM, through surge protectors, out to the LAN.
7. Remove LOTO, re-energise.

### 6.3 Verify the reads from the real contacts — **read side first**

Before trusting any command, confirm every input responds to the real switch
(`HARDWARE.md §6`):

```bash
ping -c 3 192.168.1.251
# Walk the channels; add --skip-dos so a load flip can't happen while verifying reads
atspi-bench --host 192.168.1.251 --port 502 --unit-id 1 --skip-dos
```

Then physically: press front-panel **Test** → DI 0 pulses; briefly trip the
utility breaker upstream → DI 3 drops; disable the generator → DI 4 drops and
DI 5 goes high.

**Reconcile the contact sense** you saw on the bench (jumper-to-common = 0)
against the ASCO's actual NO/NC contacts — a real NO contact may rest opposite
to your bench jumper (`NEXTSTEPS.md §6`, `HARDWARE.md §3`).

### 6.4 Commands last

Only once every read is correct, and **in a planned outage**, exercise the
relays — drop `--skip-dos` and let `atspi-bench` drive each DO while you confirm
the matching ASCO terminal responds. The bench tool exits `0` only when every
step passed.

---

## Final commissioning + go-live (`HARDWARE.md §7`)

With the deployed tag from Phase 5 already installed:

```bash
sudo systemctl enable --now atspi
sudo systemctl status atspi          # 'active (running)'

# End-to-end smoke from another OT host — must NOT match the mock's
# hardcoded [0,1,1,0,0,0] (if it does, driver: mock is still set)
modpoll -m tcp -a 1 -r 1 -c 6 <ats-pi OT IP>
```

GenWatch's ATS card should now show the **real** switch position. Complete the
commissioning checklist (`HARDWARE.md §7`) and the sign-off sheet.

### First-boot gotchas (`HARDWARE.md §7`)

| Symptom | Likely cause | Fix |
|---|---|---|
| `modpoll` returns `[0, 1, 1, 0, 0, 0]` no matter the ATS state | `driver: mock` still set | flip to `driver: adam` |
| All DIs read `0` / `position` stuck `unknown` | wrong DI function code | `io.adam.di_read: discrete_inputs` and restart |
| `modpoll` times out | Pi firewall or wrong bind | allow GenWatch→5020; set `modbus_server.host` to the OT IP |
| `systemctl status` → `status=127` | venv install path | point `ExecStart=` at the venv's `atspi` |
| `User/Group resolution: 'atspi' not found` | service user missing | `install.sh` creates it, or `useradd --system atspi` |
| GenWatch shows "(via gen telemetry)" not "(via ATS-Pi)" | `expected_unit_id` mismatch | match it to `site.unit_id` (`0x0035`) |

---

## If something breaks later

**[`RUNBOOK.md`](./RUNBOOK.md)** is the 2am field guide — `fault_summary` bit
decoding, "GenWatch can't see the ATS-Pi", stuck-relay detection, the comms-loss
release log lines, rollback, and last resorts.

## The full doc set

| Doc | When |
|---|---|
| **TUTORIAL.md** (this) | The linear start-to-finish path |
| [`FIELD-INSTALL.md`](./FIELD-INSTALL.md) | One-page wiring card for the installing electrician (Phase 6) |
| [`SIGN-OFF.md`](./SIGN-OFF.md) | Fill-in commissioning acceptance sheet (Phases 5–6) |
| [`BENCH.md`](./BENCH.md) | Bare-module bench validation (Phase 1) |
| [`NEXTSTEPS.md`](./NEXTSTEPS.md) | Staging: fail-safe, network, GenWatch (Phases 2–5) |
| [`HARDWARE.md`](./HARDWARE.md) | BOM, ASCO wiring, cabinet install (Phases 0, 6) |
| [`RUNBOOK.md`](./RUNBOOK.md) | Field troubleshooting after go-live |
| [`DEVELOPMENT.md`](./DEVELOPMENT.md) | Dev environment, mock controls, tests |
| [`SPEC.md`](./SPEC.md) | Implementation architecture & rationale |
