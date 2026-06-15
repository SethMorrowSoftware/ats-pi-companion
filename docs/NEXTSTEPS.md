# Staging verification — before the cabinet install

> Pick this up **after** [`BENCH.md`](./BENCH.md) (bare-module bench
> validation passed) and **before** [`HARDWARE.md`](./HARDWARE.md) (ASCO
> wiring at the cabinet). The goal is to prove the **entire chain — ADAM ↔
> ATS-Pi ↔ GenWatch — on the bench/staging**, with the hardware fail-safe
> configured and *physically* verified, so the only thing left to do at the
> cabinet is land wires on the ASCO. Everything that can fail at a desk
> should fail at the desk, not at 480 V.

This guide is filed under all-caps to match the repo's other docs; it's the
"NextSteps" guide.

**Prerequisites**

- A unit that passed `BENCH.md §4–§9` (reads, relays, round-trip,
  comms-loss auto-release).
- A **Windows PC** with Advantech's **Adam/Apax .NET Utility** (the only way
  to configure this ADAM-6060's fail-safe — its web UI is a dead Java
  applet, and the FSV/watchdog are not Modbus-exposed; see `BENCH.md §10`).
- A **GenWatch instance** (or a test instance) you can point at the ATS-Pi.
- Multimeter, jumper wires, the ADAM's 10–30 VDC bench supply.

**The five gates this guide closes** (record each on the sign-off):

| Gate | What it proves | Stage |
|---|---|---|
| **F1** | The ADAM physically releases a latched relay if the Pi dies | 2 |
| **F3** | Only GenWatch can reach the Modbus server (network is a safety control) | 3 |
| **F4** | A pinned, tagged build is what gets deployed | 5 |
| ICD | GenWatch reads correct state + commands round-trip | 4 |
| §8.3 | Comms-loss auto-release works end-to-end (GenWatch → relay) | 4 |

---

## Stage 1 — Configure the ADAM (Adam/Apax .NET Utility, Windows)

### 1.1 Install the utility
Download the **Adam/Apax .NET Utility** from Advantech (Support → Downloads
for the ADAM-6000 series) and install it on the Windows PC.

### 1.2 Connect to the module
The ADAM is isolated on the Pi's direct cable, so give the Windows PC a path
to it. Easiest: **move the ADAM's Ethernet to the same switch as the Windows
PC** (or direct-cable them), and set the PC's NIC to the ADAM's subnet (add a
`10.0.0.x` address if it's still at the factory `10.0.0.1`). Open the utility
→ it auto-searches and lists the module; if not, add it by IP. Default web/
config password is `00000000`.

> Put the ADAM back on the Pi's cable for Stages 2 and 4.

### 1.3 Network identity + host idle time
On the module's network/information page:
- **Static IP**: set the permanent OT address (the project default is
  `192.168.1.251`), subnet, and gateway. Update `io.adam.host` in the
  ATS-Pi config to match.
- **Host idle time (timeout): 5–10 s.** This is the Communication-WDT
  timeout — longer than a sampling blip, **shorter than the 30 s software
  watchdog** so it only fires on true host loss.

### 1.4 Fail-Safe Values (FSV) — every DO must release to OFF
On **All-Channel Configuration → Channel Setting tab** (utility §6.6.1):

The FSV is the value each digital output takes when the Communication WDT
times out. Per the manual: *"If the FSV box beside a channel is checked... the
module will set that output channel to logic **high** when a WDT timeout
occurs."*

For an ATS command relay we want the **opposite** — on comms loss every relay
must **de-energise (go to 0/OFF)** so Test / Force-Transfer / Inhibit /
Bypass *release*, not latch. So:

- **Leave the FSV box UN-checked for all six DOs** (unchecked → the fail-safe
  value is logic low → the relay drops on timeout).
- Click **Apply FSV**.

> ⚠️ **Do not trust the checkbox label — Stage 2 is what proves the
> direction.** If the cable-pull test shows a relay *holding* instead of
> dropping with FSV unchecked, this firmware's FSV does not provide
> release-on-comms-loss, and that is a **stop-ship finding** — escalate
> before going any further (the hardware fail-safe premise in
> `HARDWARE.md §5.1` would not hold for the 6060).

### 1.5 Power-on values
Confirm every DO's **power-on value is OFF** (so a power blip can't bring a
command relay up energised). `BENCH.md §7`'s power-cycle check already
verifies this from the Pi side; confirm it's explicitly set here too.

*(The Modbus map — DI coils 00001–00006, DO coils 00017–00022, `di_read:
coils` — was already confirmed in `BENCH.md §5`/§11. Nothing to change.)*

---

## Stage 2 — Cable-pull acceptance test (F1) — **HARD GATE**

This is the only test that proves the relay *physically* drops when the Pi
vanishes. Software cannot prove it. **Do not install without a recorded
pass.** ADAM back on the Pi's cable, `atspi` service stopped, watchdog
configured (Stage 1).

```bash
# 1. Latch a maintained relay directly on the ADAM (no master maintaining it):
python - <<'PY'
import asyncio
from pymodbus.client import AsyncModbusTcpClient
async def m():
    c=AsyncModbusTcpClient(host='192.168.1.251',port=502,timeout=2); await c.connect()
    await c.write_coil(address=0x12, value=True, slave=1)   # 0x12 = DO2 (Inhibit)
    print('DO2 latched ON'); c.close()
asyncio.run(m())
PY
```

2. Put a meter on DO2's contacts — closed. (The 6060 has no relay LED.)
3. **Physically pull the Pi↔ADAM Ethernet cable.**
4. Within the host-idle timeout (5–10 s) the relay must **drop** — you hear
   it click off and the meter opens.
5. Reconnect the cable and confirm `0`:

```bash
python - <<'PY'
import asyncio
from pymodbus.client import AsyncModbusTcpClient
async def m():
    c=AsyncModbusTcpClient(host='192.168.1.251',port=502,timeout=2); await c.connect()
    r=await c.read_coils(address=0x10,count=6,slave=1)
    print('DO2 after cable-pull =', int(r.bits[2]), '(must be 0)'); c.close()
asyncio.run(m())
PY
```

Because the Pi was disconnected, only the ADAM's own watchdog could have
dropped DO2 — `0` proves the fail-safe. **Record on the sign-off:** the
host-idle timeout used, that the meter opened within it, and the post-pull
read. This is the F1 acceptance gate (`HARDWARE.md §5.1/§9`).

---

## Stage 3 — ATS-Pi production identity + network safety (F3)

### 3.1 Production config
Move the ATS-Pi off the bench config to its real identity. Either run
`sudo ./install.sh` (service user + venv + systemd + `/etc/atspi/config.yaml`)
or edit the existing config. Set:

- `io.driver: adam`, `io.adam.host: <ADAM OT IP>`, `io.adam.di_read: coils`
- `site.unit_id:` the real per-site id (must equal GenWatch's
  `expected_unit_id` — see Stage 4). **Not** the default `1`/`23`.
- `modbus_server.port: 5020` (production), `modbus_server.host: <Pi OT IP>`
  (see 3.3 — **not** `0.0.0.0`)
- `persistence.state_file: /var/lib/atspi/state.json` (real path, not `/tmp`)

### 3.2 `require_hw_watchdog` stays **false** on the 6060 — and what that means
The ADAM-6060 does not expose its FSV/WDT config over Modbus, so the driver's
`require_hw_watchdog` read-back (which would refuse to drive outputs and
publish `OUTPUT_FAULT` until it confirms the fail-safe armed) **cannot run
here** — leave it `false` (`BENCH.md §10`).

Consequence to understand: with that software gate off, **nothing in the code
blocks commands on an un-armed fail-safe.** The protection is therefore
**procedural** — the Stage 2 cable-pull plus the signed-off config — not
automatic. That makes the Stage 2 record and the commissioning discipline the
actual safety control. (If you ever swap or factory-reset the ADAM, the FSV is
gone and nothing will warn you — re-run Stage 2.)

### 3.3 Bind to the OT interface + firewall allowlist (F3)
Modbus/TCP has no auth, so the network boundary *is* a safety control
(`HARDWARE.md §4.1`). Anything that can route to `:5020` can drive the switch
within the mode policy.

- **Bind** `modbus_server.host` to the Pi's specific OT-VLAN IP, not
  `0.0.0.0`.
- **Allowlist** only the GenWatch Pi:

```bash
sudo iptables -A INPUT -p tcp --dport 5020 -s <genwatch-ip> -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 5020 -j DROP
```

- **Acceptance:** from a host *off* the OT segment, `nmap -p 5020 <ats-pi-ip>`
  shows the port **filtered**. Record it. (`502` in any older notes is stale —
  the non-root service listens on `5020`.)

---

## Stage 4 — GenWatch end-to-end

The ATS-Pi from Stage 3 is now serving real ADAM state on the OT network.
Bring GenWatch into the loop. (The read scripts in `BENCH.md` were standing
in for GenWatch; now we use the real consumer.)

### 4.1 Point GenWatch at the ATS-Pi
In GenWatch's `/etc/genwatch/config.yaml`:

```yaml
ats:
  enabled: true
  host: <ats-pi OT IP>
  port: 5020                # must match modbus_server.port
  expected_unit_id: 23      # MUST equal the ATS-Pi's site.unit_id
```

Restart GenWatch. (`expected_unit_id` mismatch makes GenWatch refuse the link
silently — confirm the ids match first.)

### 4.2 Card populates + unit_id match
- GenWatch's **ATS card lights up within ~2 prime polls (~3 s)**; the load
  source annotates **"(via ATS-Pi)"**.
- Confirm the id GenWatch pins matches what the Pi reports:

```bash
modpoll -m tcp -a 1 -r 54 -c 1 <ats-pi OT IP>   # 0x0035 = site.unit_id
modpoll -m tcp -a 1 -r 49 -c 2 <ats-pi OT IP>   # ICD version (0x0030,0x0031) -> 1, 0
```

> On a bare bench (no ASCO) the **contact path** (`driver: adam`) shows
> **position = transferring** — DI0 floats high and reads as "Load Disconnect
> asserted" (`BENCH.md §8`); the real position appears once the ASCO is wired.
> (On the **hybrid** path position comes from the Group 5 bits, so it reads
> whatever the controller reports — no DI0-float artifact.)

### 4.3 Drive DI transitions, watch GenWatch follow
> **Hybrid path:** the ADAM DIs are unused — jumpering them moves nothing.
> Instead drive the **controller / switch** itself (or your Group 5 RTU
> simulator) and watch the GenWatch card follow (controller → serial → ATS-Pi →
> GenWatch). The jumper table below is the contact-path (`driver: adam`) equivalent.

Jumper DIs and confirm the **GenWatch card** tracks them — this proves the
full chain visually (contact → ADAM → ATS-Pi → GenWatch). DIs are dry-contact
(open = 1, jumpered-to-common = 0); to present a sane state on the bench:

| To show GenWatch... | Jumper to common (= 0) | Leave open (= 1) |
|---|---|---|
| **On utility** | DI0 (clear transferring), DI2 (on-emergency) | DI1 (on-normal) |
| **On generator** | DI0, DI1 | DI2 (on-emergency) |
| **Normal source lost** | + DI3 (normal-available → 0) | |
| **Engine-start calling** | | DI5 |

Move a jumper and watch GenWatch's events feed / load source update. (These
combos exercise the card; the real contact *sense* is reconciled when the
ASCO is wired — `HARDWARE.md §3`.)

### 4.4 End-to-end comms-loss auto-release (ICD §8.3)
The most important end-to-end safety test — driven from GenWatch this time,
not a script:

1. From GenWatch, **assert Inhibit** on the ATS-Pi. Confirm DO2 energises
   (meter / `modpoll -r 17 -c 6 -t 0 <adam-ip>`).
2. **Stop GenWatch** (`sudo systemctl stop genwatch`).
3. Wait **~35 s**.
4. Confirm the relay released and the read-back cleared:

```bash
modpoll -m tcp -a 1 -r 66 -c 1 <ats-pi OT IP>   # 0x0041 cmd_inhibit read-back -> 0
```

Relay dropped + register `0` = the software watchdog (`safety.py`) released a
maintained command on client silence, end-to-end. (This is the *software*
watchdog; Stage 2 was the independent *hardware* one for Pi death.)

---

## Stage 5 — Acceptance gates + sign-off (F1–F4)

Don't deploy a moving `main`. Before transporting to the cabinet:

- **Tag the exact commit** you commissioned against (e.g. `v0.1.0-rc1`) and
  deploy *only* that tag (`git checkout v0.1.0-rc1` before `install.sh`).
  `atspi --version` then matches the sign-off sheet. **(F4)**
- Commission against the **matching GenWatch tag**.
- **Record on the sign-off sheet:**
  - the deployed ATS-Pi tag + `atspi --version`,
  - **F1** — Stage 2 cable-pull result (host-idle timeout, relay dropped),
  - the ADAM config: static IP, host idle time, all FSV unchecked / power-on
    OFF,
  - **F3** — Stage 3 `nmap` filtered check,
  - ICD / §8.3 — Stage 4 card-populate, unit_id match, end-to-end release.

**Go/no-go for cabinet install:** all five gates recorded as pass, with F1
verified on the real unit.

---

## Stage 6 — Then the cabinet (`HARDWARE.md`)

Only now go to the ATS. What remains is physical and is covered in
[`HARDWARE.md`](./HARDWARE.md):

- LOTO, mount, land DI/DO wires per `HARDWARE.md §3` (verify ASCO terminal
  labels against operator's manual `381333-289`).
- **Reconcile the contact sense** you saw on the bench (jumper-to-common = 0)
  against the ASCO's actual NO/NC contacts — `HARDWARE.md §3`/`§6`.
- Re-run `testadam.sh` against the wired switch, read side first, commands
  last, in a planned outage.
- Final commissioning checklist: `HARDWARE.md §7`.

---

## Verification checklist (staging)

- [ ] **Stage 1** ADAM: static IP set, host idle time 5–10 s, all FSV
      unchecked + Apply FSV, power-on values OFF
- [ ] **Stage 2 (F1)** cable-pull: latched relay physically drops within the
      timeout — **recorded**
- [ ] **Stage 3** production config (`driver: adam`, real `site.unit_id`,
      `require_hw_watchdog: false`, real state path)
- [ ] **Stage 3 (F3)** bound to OT IP + firewall allowlist + `nmap` filtered
- [ ] **Stage 4** GenWatch card populates; `expected_unit_id` == `site.unit_id`
- [ ] **Stage 4** DI jumper transitions track on the GenWatch card
- [ ] **Stage 4 (§8.3)** assert Inhibit from GenWatch → stop GenWatch → relay
      releases in ~30 s
- [ ] **Stage 5 (F4)** commissioned commit tagged; sign-off sheet complete

When every box is checked, the electronics + software + dashboard are proven
as a system. The cabinet visit is then just wiring.
