# Bench bring-up: ADAM-6060 on the desk (before the ASCO)

This is the procedure for validating a bare **ADAM-6060** against the
`atspi` service **before any ASCO is wired** — module on the bench, Pi on
the desk, jumper wires and a multimeter. It complements
[`HARDWARE.md`](./HARDWARE.md), which assumes the switch is already wired;
this one starts from "the module just arrived in the box."

> **Scope: this doc validates the ADAM (control) half only.** For
> `driver: adam` that is the whole I/O path, and §4–§12 below cover it. For
> **`driver: hybrid`** the ADAM still drives the relays (so §7 relays, §9
> comms-loss, and §10 FSV/cable-pull apply unchanged), but switch position,
> source availability, and engine-start are read from the **ASCO Group 5
> controller over RS-485**, *not* from the ADAM DIs. The DI checks in §5,
> §6, and §8 exercise only the ADAM read path, which **hybrid does not use
> for monitoring** — so on a hybrid build you must **also** bench-verify the
> ASCO serial read path (`io.asco_serial` + its register map) per
> [`HARDWARE.md §3.1`](./HARDWARE.md). The §12 checklist has a hybrid row
> for it.

The goal is to prove, in order:

1. the Pi can reach the ADAM over Modbus,
2. **reads work** — and which Modbus function code the DIs answer on
   (`io.adam.di_read`),
3. **writes work** — each relay actuates,
4. the **full service** serves correct ICD registers from live hardware, and
5. the **comms-loss safety watchdog** drops a maintained relay on its own.

Everything here runs over a direct Pi↔ADAM Ethernet link; no ASCO, no
GenWatch. GenWatch does **not** need to be running for any of this — the
read scripts below stand in for it.

> **Already verified on real hardware** (commissioning bench, ADAM-6060,
> firmware reading as the Ed.9 manual maps it). The unit-specific findings
> are called out as **[finding]** and collected in §11.

## 0. What you need

- ADAM-6060, Raspberry Pi 5 (Raspberry Pi OS 64-bit, SSH enabled)
- A **10–30 VDC** supply for the ADAM (24 V ideal; a 12 V bench supply is
  fine). Lands on `+Vs` / `GND`. Ripple < 200 mVpp.
- One Ethernet cable (direct Pi↔ADAM works — Pi 5 auto-MDIX)
- A few **jumper wires** (to fake DI contacts) and a **multimeter**
  (continuity, to confirm relays). **[finding]** The ADAM-6060 has only
  system/status LEDs — **no per-channel I/O LEDs** — so the relay
  read-back (below) and a meter are how you confirm outputs, not lights.

## 1. Power the ADAM

Land the DC supply on `+Vs` / `GND` (10–30 VDC). The status LED comes up;
a **blinking** status LED on a freshly-powered, not-yet-talking module is
normal idle state, not a fault. (Solid red for 30 s is the utility's
"locate" function — unrelated.)

## 2. Network: reach the ADAM

The ADAM ships at **`10.0.0.1` / 255.255.255.0**, Modbus TCP on **502**,
web password **`00000000`**.

```bash
# On the Pi, put its wired port on the ADAM's subnet:
sudo ip addr add 10.0.0.2/24 dev eth0      # (eth0 = wired port; `ip -br link` to confirm)
ping -c3 10.0.0.1
```

Replies = the Pi and ADAM are talking. (No reply → check the RJ45 link
lights and that `ip addr show eth0` shows `10.0.0.2/24` / `state UP`.)

> **[finding] The web UI is a dead end on a modern browser.** This firmware
> serves a **Java applet** (`Adam6060.class` / `Adam6060.jar`) — modern
> browsers can't run it, so `http://10.0.0.1` is a blank page. Network and
> fail-safe configuration is done with Advantech's **Adam/Apax .NET
> Utility** (Windows) — see §10. None of the bench validation below needs
> the web UI.

## 3. Pi: OS + service (dev venv)

For the bench, the editable dev install gives you `atspi`, `atspi-bench`,
and lets `testadam.sh` find them:

```bash
sudo apt update && sudo apt install -y python3-venv git
git clone https://github.com/SethMorrowSoftware/ats-pi-companion.git
cd ats-pi-companion
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
atspi --version
```

Keep this terminal's venv active. (A fresh shell:
`cd ~/ats-pi-companion && source .venv/bin/activate`.)

## 4. Is it alive? ping + live snapshot

```bash
./testadam.sh --host 10.0.0.1
```

This pings, prints a live snapshot of all 6 DIs + 6 relays, then starts the
interactive walkthrough. If the snapshot prints, the Modbus path is good.
Press `Ctrl-C` at the first DI prompt if you only wanted the snapshot.

## 5. Confirm `di_read` (coils vs discrete_inputs)

> **adam driver only** — in hybrid the DIs are unused, so this proves
> nothing about the served state (which comes from the serial path). Skip
> on a hybrid bench and verify `io.asco_serial` instead (HARDWARE.md §3.1).

The one setting that must be confirmed per unit: do the DIs answer on
`coils` (FC01) or `discrete_inputs` (FC02)?

**[finding] On the ADAM-6060, the DIs are coils.** Confirmed both on the
bench and in the manual (Appendix B.2.8): DI value = coils **00001–00006**,
DO value = coils **00017–00022**. So `io.adam.di_read: coils` (the default)
is correct — no change. If a future unit reads all-`0`/all-`1` no matter
what you jumper, re-run with `--di-read discrete_inputs`.

The DIs are **dry-contact**: an open input reads **1**, shorting the channel
to its DI-common/GND reads **0**. Un-wired DIs sit at `1`.

## 6. Verify the DIs (jumper technique)

> **adam driver only** — in hybrid the DIs are unused and these jumper
> checks prove nothing about the served state.

There's no ASCO, so you simulate each contact with a jumper. Run the
walkthrough and, at each DI prompt, bridge that channel to its DI common,
then press Enter:

```bash
./testadam.sh --host 10.0.0.1
```

The tool reads a baseline, you actuate, it confirms **exactly that channel**
changed.

> **Gotcha (expected):** if you *move* one jumper straight from channel N to
> N+1, the tool flags `FAIL: channels [N, N+1] changed` — it saw the old
> channel spring back to open *and* the new one close. That's the test
> procedure, not a wiring fault; each DI is responding correctly. For a clean
> all-green sweep, **remove the jumper after each channel** so only one
> channel differs between baseline and read.

## 7. Verify the relays (read-back, not lights)

The ADAM-6060 has no per-channel relay LEDs, so confirm outputs two ways:
the ADAM's own Modbus **read-back** (how the service's stuck-relay detection
sees the truth) and a **meter** on the contacts. `atspi-bench`'s pulses are
too quick to meter, so use this helper, which drives one relay at a time and
**holds** it:

```bash
cat > ~/relaytest.py <<'PY'
import asyncio
from pymodbus.client import AsyncModbusTcpClient

HOST, UNIT, DO_BASE = "10.0.0.1", 1, 0x0010
NAMES = ["DO0 test", "DO1 force-transfer", "DO2 inhibit",
         "DO3 bypass", "DO4 spare", "DO5 spare"]

async def show(c):
    rb = await c.read_coils(address=DO_BASE, count=6, slave=UNIT)
    print("    relay read-back:", [int(b) for b in rb.bits[:6]])

async def main():
    c = AsyncModbusTcpClient(host=HOST, port=502, timeout=2)
    if not await c.connect():
        print("could not connect to", HOST); return
    print("at rest:"); await show(c)
    for ch in range(6):
        input(f"\n>>> Enter to CLOSE {NAMES[ch]} ...")
        await c.write_coil(address=DO_BASE + ch, value=True, slave=UNIT)
        await show(c)
        input("    listen for the click / meter the 2 terminals, then Enter to OPEN ...")
        await c.write_coil(address=DO_BASE + ch, value=False, slave=UNIT)
        await show(c)
    c.close()
    print("\ndone — all relays back to 0")

asyncio.run(main())
PY
python ~/relaytest.py
```

Each relay is good when the read-back flips its position `0 → 1 → 0`, you
hear the click, and (if metered) continuity closes while driven.

> **Check the rest state.** All relays must read `0` at rest, **including
> after a power-cycle** — power the ADAM off/on and re-read. A DO that comes
> up `1` on its own means its power-on value is set ON (a safety issue for a
> command relay); fix it in §10. On the validated unit, a DO read `1` at
> rest once from leftover state, cleared with a write, and came up `0` after
> a clean power-cycle — i.e. power-on defaults were fine.

## 8. Full service round-trip (`driver: adam`)

Now run the real service against the ADAM and prove a physical contact flows
through to the served register. Make a bench config:

```bash
cp config.example.yaml config.yaml
sed -i 's/^  driver: mock/  driver: adam/'                                config.yaml
sed -i 's/^    host: 192.168.1.251/    host: 10.0.0.1/'                   config.yaml
sed -i 's/^    require_hw_watchdog: true/    require_hw_watchdog: false/' config.yaml
sed -i 's/^  port: 502/  port: 5020/'                                     config.yaml   # non-root
sed -i 's#^  state_file: /var/lib/atspi/state.json#  state_file: /tmp/atspi-state.json#' config.yaml
grep -E '^  driver:|^    host:|^    require_hw_watchdog:|^  port:' config.yaml
```

`require_hw_watchdog: false` is the **explicit bench waiver** — required on
the ADAM-6060 regardless (see §10). Start the service in the background:

```bash
atspi --config config.yaml --log-level INFO > ~/atspi.log 2>&1 &
sleep 3; tail -n 25 ~/atspi.log     # expect: connected to 10.0.0.1, server on 5020, no errors
```

Read the **served** ICD core state (the registers GenWatch reads — holding
registers on the Pi, not the ADAM):

```bash
cat > ~/readstate.py <<'PY'
import asyncio
from pymodbus.client import AsyncModbusTcpClient
POS={0:'utility',1:'generator',2:'transferring',3:'unknown'}
MODE={0:'auto',1:'manual',2:'test',3:'unknown'}
async def m():
    c=AsyncModbusTcpClient(host='127.0.0.1',port=5020,timeout=2)
    await c.connect()
    v=(await c.read_holding_registers(address=0,count=6,slave=1)).registers
    print('position           :',v[0],POS.get(v[0]))
    print('normal_available   :',v[1])
    print('emergency_available:',v[2])
    print('engine_start       :',v[3])
    print('ats_mode           :',v[4],MODE.get(v[4]))
    print('fault_summary      :',v[5])
    c.close()
asyncio.run(m())
PY
python ~/readstate.py
```

Jumper **DI3** (normal source available), wait ~1 s (debounce), re-run:
`normal_available` flips **`1 → 0`**. Remove the jumper → back to `1`. That
is the whole chain: contact → driver → register store → served register.

> **adam driver only** — DI3 → `normal_available` is the ADAM read path. In
> hybrid the DIs are unused and this proves nothing about the served state;
> there `normal_available` is derived from the serial `normal_available_bit`,
> bench-verified per HARDWARE.md §3.1.

> **Expected on a bare bench (adam path only):** `position` reads
> **`transferring`**, not `unknown`. DI0 (Load Disconnect) floats to `1`
> (open), and the `adam` driver reads DI0=`1` as "transfer in progress," so
> it holds `transferring` while nothing is wired. Not a fault.
> `fault_summary: 0` is what you want. **This is specific to the ADAM DI
> read** — the hybrid serial reader derives position from the
> `on_normal`/`on_emergency` bits, so a bare serial bench with the switch
> sitting on utility reads **`utility`**, not `transferring`.

## 9. Safety auto-release (comms-loss watchdog, ICD §8.3)

The most important software behavior: if the client goes silent for 30 s
while a maintained command is asserted, the service releases it — in the
store **and** on the relay. This asserts Inhibit (DO2), goes silent, and
watches DO2 drop on its own:

```bash
cat > ~/safetytest.py <<'PY'
import asyncio, time
from pymodbus.client import AsyncModbusTcpClient

async def main():
    svc  = AsyncModbusTcpClient(host="127.0.0.1", port=5020, timeout=2); await svc.connect()
    adam = AsyncModbusTcpClient(host="10.0.0.1",  port=502,  timeout=2); await adam.connect()

    async def do2():
        r = await adam.read_coils(address=0x10, count=6, slave=1)
        return int(r.bits[2])

    # re-arm the watchdog with a read, then assert Inhibit (drives DO2)
    await svc.read_holding_registers(address=0, count=1, slave=1)
    await svc.write_register(address=0x0101, value=1, slave=1)
    await asyncio.sleep(0.5)
    print("Inhibit asserted -> ADAM DO2 =", await do2(), "(expect 1)")

    svc.close()  # silent on the service; the 30 s clock starts now
    print("silent on the service -- watchdog should drop DO2 in ~30 s:")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 40:
        await asyncio.sleep(3)
        print(f"  {time.monotonic()-t0:4.0f}s silent ... ADAM DO2 =", await do2())
    adam.close()

asyncio.run(main())
PY
python ~/safetytest.py
```

DO2 holds `1`, then drops to `0` at ~30 s — and you hear the relay click
off. Confirm the service logged it:

```bash
grep -iE 'silent|auto-releas' ~/atspi.log | tail -2
```

Stop the bench service when done: `kill %1` (all relays are at `0` — clean).

## 10. The hardware fail-safe (FSV / Communication WDT)

This is the production backstop for **"the Pi itself dies with a relay
latched"** — the software watchdog in §9 can't cover that (it dies with the
process). On the ADAM-6000 series the feature is the per-channel
**Fail-Safe Value (FSV)** applied by the **Communication WDT** after the
**host idle time** (manual §6.6.1).

> ### [finding] On the ADAM-6060 the FSV/WDT is **not reachable over Modbus**
>
> The 6060's Modbus map (Appendix B.2.8) exposes only DI/DO coils, counters,
> pulse-width, GCL, and module name — **no FSV, host-idle-time, or WDT
> registers** in either the coil or holding-register space, and they aren't
> in the ASCII command set either. They are configurable **only** through the
> Adam/Apax .NET Utility. Two consequences:
>
> 1. **`io.adam.require_hw_watchdog` must be `false` on the 6060.** That
>    feature reads the watchdog/safety config back via FC03 holding registers
>    to confirm the fail-safe is armed — but those registers don't exist
>    here, so it would always fail-closed and refuse to drive outputs. On this
>    hardware the fail-safe is verified by the **cable-pull test below**, not
>    by software read-back. (`HARDWARE.md §5.1/§5.2` assume the config is
>    Modbus-readable; that holds for some Advantech families but **not** the
>    ADAM-6060.)
> 2. **Configuring the FSV needs the Windows .NET Utility** (or it run under
>    Wine/Mono) — there is no Linux/Modbus path.

### Configure it (Adam/Apax .NET Utility, Windows)

1. Connect the configuring PC to the ADAM's network (10.0.0.x), open the
   utility, find the module.
2. **Network settings:** set **Host Idle (timeout)** to **5–10 s** — longer
   than a sampling blip, shorter than the 30 s software watchdog so it only
   fires on true host loss.
3. **Digital-output / FSV config:** the **FSV** for **every** DO must produce
   **0 / OFF** (relay de-energised) on timeout, so a Pi crash *releases*
   Test / Force-Transfer / Inhibit / Bypass rather than latching them.
   - **Polarity gotcha:** in the utility a **checked** FSV box drives that
     output **HIGH** on timeout. For "release the relays" you want the boxes
     **un-checked**. Confirm the direction with the cable-pull test — don't
     trust the checkbox label.

### Cable-pull test (the real acceptance test — F1)

Software cannot prove the relay physically drops; this is the only test that
does. With the watchdog configured and the `atspi` service stopped:

```bash
# 1. Directly latch a maintained relay on the ADAM (no master maintaining it):
python - <<'PY'
import asyncio
from pymodbus.client import AsyncModbusTcpClient
async def m():
    c=AsyncModbusTcpClient(host='10.0.0.1',port=502,timeout=2); await c.connect()
    await c.write_coil(address=0x12, value=True, slave=1)   # DO2 = Inhibit
    print('DO2 latched ON'); c.close()
asyncio.run(m())
PY
# 2. (meter DO2's contacts, hear it closed)
# 3. PHYSICALLY PULL the Pi<->ADAM Ethernet cable.
# 4. Within the host-idle timeout the relay must drop — you hear it click off.
# 5. Reconnect the cable and confirm DO2 reads 0:
python - <<'PY'
import asyncio
from pymodbus.client import AsyncModbusTcpClient
async def m():
    c=AsyncModbusTcpClient(host='10.0.0.1',port=502,timeout=2); await c.connect()
    r=await c.read_coils(address=0x10,count=6,slave=1)
    print('DO2 after cable-pull =', int(r.bits[2]), '(must be 0)'); c.close()
asyncio.run(m())
PY
```

Because the Pi was disconnected, only the ADAM's own watchdog could have
dropped DO2 — `0` proves the fail-safe works. **Record this on the
commissioning sign-off** (`HARDWARE.md §9`); it is the F1 acceptance gate.

## 11. Findings summary (this hardware)

| # | Finding |
|---|---|
| 1 | DIs answer on **coils (FC01)**: DI 00001–00006, DO 00017–00022. `io.adam.di_read: coils` (default) is correct — manual-confirmed (App. B.2.8). |
| 2 | **No per-channel I/O LEDs** — confirm relays by Modbus read-back + a meter, not lights. |
| 3 | The **web UI is a Java applet** — dead on modern browsers. Use the .NET Utility for config. |
| 4 | **FSV / Communication WDT / host-idle-time are not Modbus-exposed.** `io.adam.require_hw_watchdog` cannot be used on the 6060 — keep it **`false`** and verify the fail-safe with the §10 cable-pull test. |
| 5 | **adam path only:** on a bare bench `position` reads **`transferring`** (DI0 floats high = "Load Disconnect asserted"); expected, not a fault. The hybrid serial reader derives position from the `on_normal`/`on_emergency` bits, so a bare serial bench with the switch on utility reads **`utility`**, not `transferring`. |

## 12. Bench sign-off checklist

- [ ] §4 ping + Modbus snapshot OK
- [ ] §5 `di_read` confirmed (`coils`)
- [ ] §6 all 6 DIs track their jumper, correct channel
- [ ] §7 all 6 relays drive + read back; all rest at `0` after power-cycle
- [ ] §8 service starts clean on `driver: adam`; DI jumper → served register
- [ ] **hybrid only:** serial link up + Group 5 map bench-verified
      (HARDWARE §3.1) — served position/availability tracks the switch over
      the serial path (the ADAM DI checks above don't cover this)
- [ ] §9 comms-loss auto-release drops the relay at ~30 s
- [ ] §10 FSV configured (Windows utility) **and cable-pull verified** —
      *deferred to commissioning; the bench proves everything else*

Once §4–§9 pass, the unit is bench-validated. Next is staging verification —
the §10 hardware fail-safe via the .NET Utility, GenWatch end-to-end, and the
acceptance gates — in [`NEXTSTEPS.md`](./NEXTSTEPS.md), then the ASCO wiring
and production install in [`HARDWARE.md`](./HARDWARE.md).
