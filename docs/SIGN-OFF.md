# Commissioning sign-off — ATS-Pi

Fill this in at commissioning and keep it with the site records. **Go-live
requires every gate PASS, with F1 verified on the real unit.** Re-run the F1
cable-pull after any ADAM swap or factory reset — nothing in software will warn
you the fail-safe is gone.

```
Site / ATS tag : ______________________   Date : ____________
Commissioned by: ______________________   Witness : ____________
```

## Build (F4 — deploy a tag, never a branch/`main`)

```
 atspi --version          : ______________   (must match the deployed tag)
Deployed git tag          : ______________   e.g. v0.1.0-rc1
GenWatch version / tag    : ______________
```

- [ ] **F4** — deployed from a **tagged** commit (not a dev branch), re-run via `install.sh`

## Network identity (these MUST match across both ends)

```
ATS-Pi OT IP   : ______________      ADAM OT IP : ______________
GenWatch IP    : ______________
modbus_server.port : __________  ==  GenWatch ats.port : __________
site.unit_id       : __________  ==  GenWatch expected_unit_id : __________
io.adam.di_read    : coils       io.adam.require_hw_watchdog : false   (6060)
```

## Gates

**F1 — hardware fail-safe (the only thing that releases a latched relay if the Pi dies)**
```
host-idle / comms-WDT timeout : ______ s   (must be 5–10 s)
all DO Fail-Safe Values → OFF on timeout : ☐
DO power-on values → OFF                 : ☐
```
- [ ] **F1 CABLE-PULL** — DO2 latched directly on the ADAM, Pi↔ADAM cable
      **physically pulled**, relay **dropped within the timeout** (metered /
      heard). *Software cannot prove this; the physical drop is the gate.*
      Result witnessed by: ______________

**F3 — network is a safety control (Modbus/TCP has no auth)**
- [ ] `modbus_server.host` bound to the **OT IP** (not `0.0.0.0`)
- [ ] firewall: only **GenWatch → ats-pi:`<port>`** allowed; all other dropped
- [ ] `nmap -p <port> <ats-pi>` from **off** the OT segment shows **filtered**

**ICD — GenWatch reads correct state**
- [ ] ATS card populated, load source annotated **"(via ATS-Pi)"** (not "(via gen telemetry)")
- [ ] `site.unit_id` == GenWatch `expected_unit_id`
- [ ] ICD version reads `[1, 0]`

**§8.3 — comms-loss auto-release (end to end)**
- [ ] Assert **Inhibit from GenWatch** → confirm DO2 energized
- [ ] **Stop GenWatch** → relay **released within ~30 s** (`auto-releasing … per ICD §8.3` in the journal)

## Read verification — each DI against the REAL switch

- [ ] DI0 Test pulse   - [ ] DI1 on-normal   - [ ] DI2 on-emergency
- [ ] DI3 utility-available   - [ ] DI4 gen-available   - [ ] DI5 engine-start
- [ ] **Contact sense reconciled** (bench `open=1` vs the real NO/NC rest states)

## Command verification — planned outage

- [ ] DO0 Test   - [ ] DO1 Force-Transfer   - [ ] DO2 Inhibit   - [ ] DO3 Bypass

## ADAM config record (for the next tech)

```
ADAM static IP : ____________   host-idle timeout : ______ s
all DO FSV → OFF : ☐    power-on values OFF : ☐    di_read : coils
```

---

```
GO / NO-GO :  ☐ GO   (all gates PASS, F1 verified on the real unit)

Signature : ____________________________   Date : ____________
```
