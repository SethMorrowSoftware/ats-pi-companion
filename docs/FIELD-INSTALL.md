# Field wiring card — ATS-Pi ↔ ASCO Series 300

**Print this page.** It is everything the installing electrician needs at the
cabinet: land power, land the signal wires per the two tables, re-energize,
then the commissioning tech verifies reads **before anyone drives the switch.**
Deep reference is [`HARDWARE.md`](./HARDWARE.md); you should not need it.

---

## ⚠️ Safety — read first

- **LOTO utility AND generator** before opening the cabinet. ATS internals are
  **480 V / 600 A**. Qualified electrician + NFPA 70E PPE.
- The wiring itself is non-invasive — you only land wires on **existing
  customer terminals** — but you are inside a live-capable cabinet.
- 🔴 **Verify every ASCO terminal number on this card against operator's manual
  `381333-289` for THIS unit's catalog number.** Pin numbers vary by catalog #.
  If a label doesn't match the card, **STOP and call the integrator.**

## What you're landing

> **⚠️ Which input path? Confirm with the integrator before Step 3.** This card's
> INPUT wiring (**Step 3**) and DI read-verify (**Step 6**) are for the
> **contact-only** path (`driver: adam`): six dry contacts from the 18RX +
> 14AA/14BA accessories. If this site uses the **hybrid** path
> (`driver: hybrid`), those accessories are **not** installed and the ADAM DIs
> are unused — the input side is a **single USB-RS485 cable** from the ASCO
> Group 5 controller. Do **Step 3-H** instead of Step 3, and verify reads over
> serial (see the Step 6 note). **Power (1), network (2), OUTPUT wiring (4), and
> re-energize (5) are identical either way.**

A small enclosure (Raspberry Pi + **ADAM-6060** I/O module + 24 VDC PSU) beside
the ATS. Ten signal wires total (contact path), plus power and two network drops:

```
   ASCO 300                 ADAM-6060                  (network)
  ┌────────┐   6 inputs    ┌──────────┐   Cat6        ┌──────────┐
  │ dry    │ ───────────▶  │ DI0..DI5 │ ───────────▶  │ OT switch│──▶ Pi ──▶ GenWatch
  │ contacts│              │          │               └──────────┘
  │        │   4 outputs   │ RL0..RL3 │
  │ command│ ◀───────────  │ relays   │
  └────────┘               └──────────┘
                            ▲ +Vs/GND
                            │ 24 VDC
                         ┌──┴───┐
                         │ PSU  │◀── 120 VAC
                         └──────┘
```

*(**Hybrid path:** the six dry-contact inputs above are replaced by one
USB-RS485 cable from the Group 5 controller to the Pi — see **Step 3-H**. The
output relays and everything else are unchanged.)*

## Materials / tools

22 AWG stranded control wire + ferrules · ferrule crimper · small flat
screwdriver · **multimeter** · 2× Cat6 + 2× surge protector · the
Pi/ADAM/DR-30-24-PSU enclosure on DIN rail.

---

## Step 1 — Power (do this BEFORE signal wires)

- [ ] 120 VAC: **L → PSU L**, **N → PSU N**, **G → PSU ground**. Fuse the branch at **1 A**.
- [ ] PSU output: **+24 V → ADAM `+Vs`**, **0 V → ADAM `GND`**. Meter it: **24 VDC**, correct polarity.
- [ ] ADAM status LED lights (a **blinking** LED on an idle module is normal).

## Step 2 — Network

- [ ] **ADAM** RJ45 → surge protector → OT switch
- [ ] **Pi** RJ45 → surge protector → OT switch

*(IP setup is the integrator's job — you just land the cable.)*

## Step 3 — INPUT wires: ASCO contacts → ADAM DI

The ADAM inputs are **dry-contact**: land each ASCO contact **between its `DIn`
terminal and the ADAM's DI common** (the shared input common — verify the label
on the module's wiring legend; commonly `DI.GND`). Run the DI common to one
point; every contact's return leg ties there.

- [ ] Land the **ADAM DI common** to your common point first.

| ✓ | ADAM input | ASCO source contact | Confirms |
|---|---|---|---|
| ☐ | **DI0** ↔ DI-common | Load Disconnect, terminals **1 ↔ 2** | transfer in progress |
| ☐ | **DI1** ↔ DI-common | Aux **14AA** NO contact | "On Normal" (on utility) |
| ☐ | **DI2** ↔ DI-common | Aux **14BA** NO contact | "On Emergency" (on generator) |
| ☐ | **DI3** ↔ DI-common | **18RX RL6** NO contact | utility source available |
| ☐ | **DI4** ↔ DI-common | **18RX RL5** NO contact | generator source available |
| ☐ | **DI5** ↔ DI-common | engine-start (parallel sense) | ATS calling the generator |

🔴 **DI5 is special.** It's a **parallel sense on the existing engine-start wire
to the H-100**, not a spare dry contact. **Meter it first** — if you read a
voltage, it is NOT a dry contact: **STOP and call the integrator.** Do not
disturb the existing engine-start wire.

## Step 3-H — INPUT for the hybrid path (RS-485 serial — instead of Step 3)

On the **hybrid** path there are **no** 18RX / 14AA / 14BA contacts and the ADAM
DIs are unused. Land **one** serial run instead of the six DI wires above:

- [ ] **USB-RS485 adapter** into the Pi's USB. (Integrator confirms which
      `/dev/ttyUSBx` it becomes.)
- [ ] **RS-485 from the ASCO Group 5 controller** to the adapter: **A ↔ A**,
      **B ↔ B**, plus **signal ground** — a differential pair, *not* RS-232, so
      a straight PC serial cable will not work. The controller's port is a DB9
      or a `Y Z B A 24 GND` block; **confirm the pinout against `381339-221`**
      for THIS unit before landing.
- [ ] If the link is dead, **swap A and B** first (the most common RS-485
      fault). On a long cabinet run, enable the adapter's **120 Ω termination**.

🔴 The Group 5 register/bit map and the controller's RS-485 address/baud are the
**integrator's** bench-verified settings — you only land the cable.

## Step 4 — OUTPUT wires: ADAM relays → ASCO command inputs

> 🏷️ **Label check — "RL" *is* the output.** The ADAM silkscreens these
> outputs **`RL0`–`RL5`** ("RL" = relay) — the same channels the software and
> this card call **`DO0`–`DO5`**. `DO0` = the `RL0` terminals, `DO1` = `RL1`,
> and so on, one-to-one. (The `DI` side matches this card as-is.)

Each ADAM relay is a **dry, normally-open contact** — a volt-free switch, **no
polarity**: land its two terminals across the two ASCO input terminals (if a
channel has three terminals — COM/NO/NC — use **COM + NO**).

| ✓ | ADAM relay (silkscreen = card) | ASCO command input | Function |
|---|---|---|---|
| ☐ | **RL0** (= DO0) | terminals **6 – 7** | Test (momentary) |
| ☐ | **RL1** (= DO1) | terminals **8 – 9** | Force Transfer (maintained) |
| ☐ | **RL2** (= DO2) | terminals **10 – 11** | Inhibit (maintained) |
| ☐ | **RL3** (= DO3) | terminals **12 – 13** | Bypass time delay (momentary) |

⛔ **DO NOT** land anything on ASCO terminals **14, 15, or 16**, and leave ADAM
**RL4 / RL5** (= DO4 / DO5) unused. Those are factory-use — driving them can
**damage the controller**.

## Step 5 — Re-energize

- [ ] Remove LOTO per site procedure; restore utility and generator sources.
- [ ] Confirm PSU 24 VDC and ADAM LED.

---

## Step 6 — VERIFY READS (commissioning tech, before ANY relay is driven)

> **Hybrid path:** the DI table below is for the contact-only path — the ADAM
> DIs aren't used. The tech instead confirms the **serial** read: that
> `position` / availability / engine-start track the real switch over the Group
> 5 link (`HARDWARE.md §3.1`), then goes straight to Step 7 (the OUTPUT side is
> the same).

Read-side first. From the Pi (`<ADAM-IP>` = the ADAM's address):

```
atspi-bench --host <ADAM-IP> --skip-dos
```

`--skip-dos` keeps it **read-only** — it will not drive the switch. Actuate each
contact and confirm the matching channel changes:

| Physical action | Expect |
|---|---|
| Press the ATS front-panel **TEST** briefly | DI0 pulses |
| Switch resting on utility | DI1 active, DI2 not |
| Trip the utility breaker upstream (**briefly!**) | DI3 drops |
| Disable the generator | DI4 drops, DI5 asserts |

🔴 **Reconcile contact sense.** The bench used jumper-to-common (open = `1`). The
real 18RX / 14AA / 14BA contacts have their own NO/NC rest states — confirm
"utility present" actually reads the way the software expects **before trusting
`position`.** If a channel reads backwards, **note it for the integrator** (it's
a driver/contact-sense setting) — do **not** swap wires to "fix" it unless the
integrator directs.

## Step 7 — COMMANDS LAST (planned outage only)

Only after **every read is correct**, and in a **planned outage**:

```
atspi-bench --host <ADAM-IP>
```

This drives each relay (Test / Force-Transfer / Inhibit / Bypass) and prompts
you to confirm the ASCO responded. Then complete [`SIGN-OFF.md`](./SIGN-OFF.md).

---

## 🛑 STOP and call the integrator if:

- An ASCO terminal label doesn't match `381333-289`.
- **DI5 engine-start measures a voltage** (it must be a dry/volt-free closure).
- A DI reads **backwards** after wiring.
- The ADAM won't power, or its LED stays **solid red > 30 s** (that's the
  utility's "locate," not a fault — but >30 s solid with no comms, escalate).
- Anything on this card is ambiguous. **Do not improvise on a switch this size.**

> Integrator: full commissioning sequence and the five first-boot gotchas are in
> [`HARDWARE.md §7`](./HARDWARE.md); end-to-end staging is
> [`NEXTSTEPS.md`](./NEXTSTEPS.md); the start-to-finish path is
> [`TUTORIAL.md`](./TUTORIAL.md).
