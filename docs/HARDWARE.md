# Hardware: BOM, wiring, install

This is a condensed Path-B-style install guide tailored for the ATS-Pi
deployment. For the full reference (with rationale and alternative
hardware options), see GenWatch's
[`docs/integrations/asco-series-300.md`](https://github.com/SethMorrowSoftware/GenWatch/blob/main/docs/integrations/asco-series-300.md).

## 1. Bill of materials

| # | Part | Qty | Approx. (USD) | Purpose |
|---|------|-----|---------------|---------|
| 1 | Raspberry Pi 5 (4 GB) + microSD + case + PSU | 1 | $130 | The ATS-Pi itself |
| 2 | Advantech **ADAM-6060** (6 DI + 6 relay out, Modbus TCP) | 1 | $400 | Physical I/O front-end |
| 3 | Mean Well **DR-30-24** (24 VDC DIN-rail PSU) | 1 | $50 | Powers the ADAM |
| 4 | DIN-rail, end stops, ferrules | 1 lot | $40 | Mounting |
| 5 | Cat6 patch cables + Ubiquiti **ETH-SP-G2** surge protector | 2 | $40 | LAN drops for Pi and ADAM |
| 6 | 22 AWG stranded control wire | 1 spool | $30 | Field wiring |
| 7 | ASCO **18RX REX module** (kit 935148) — if not already installed | 1 | $400-600 | Source-availability contacts (RL5, RL6) |
| 8 | ASCO **14AA/14BA aux contact kit** — if not already installed | 1 | $200-400 | Position contacts |

**Recommended setup**: Pi and ADAM mounted in a small enclosure beside
the ATS cabinet, both powered from a single 120 VAC branch fused at
1 A, both on the OT VLAN sharing the network with GenWatch.

## 2. Pre-install survey

Before ordering anything, open the ASCO cabinet (with LOTO) and check
what accessories are already installed:

- **18RX REX module** — small relay board, usually mounted near the
  Group 5 controller with terminals labeled RL5/RL6 and a green LED
- **14AA/14BA aux contacts** — auxiliary contact blocks on the switch
  mechanism with field wires running to a separate terminal strip
- **Engine-start wire** — TB labeled "3", "4" or similar, running to
  the H-100 — DO NOT disturb this

If the 18RX or aux contacts are missing, order them and install during
your next planned ATS outage before starting integration work.

## 3. ATS terminal mapping

The complete ATS terminal mapping per ICD Appendix A. Each row below
becomes a single channel on the ADAM-6060.

### Inputs (ADAM DIs read from ATS)

| ADAM channel | ASCO source | Reads |
|---|---|---|
| DI 0 | Load Disconnect contact (terminals 1↔2) | momentary pulse during transfer (drives ICD `position=transferring`) |
| DI 1 | Aux 14AA NO contact | "On Normal" position |
| DI 2 | Aux 14BA NO contact | "On Emergency" position |
| DI 3 | 18RX RL6 NO contact | Normal source available |
| DI 4 | 18RX RL5 NO contact | Emergency source available |
| DI 5 | Engine-start contact (sense in parallel with H-100 wire) | ATS asserting engine-start to H-100 |

### Outputs (ADAM relay outputs drive ATS inputs)

| ADAM channel | ASCO destination | Drives |
|---|---|---|
| DO 0 | Momentary Test Switch (terminals 6-7) | ICD `cmd_test` — ≥ 500 ms pulse |
| DO 1 | Maintained Transfer (terminals 8-9) | ICD `cmd_force_transfer` (maintained) |
| DO 2 | Inhibit Transfer (terminals 10-11) | ICD `cmd_inhibit` (maintained) |
| DO 3 | Bypass Transfer Time Delay (terminals 12-13) | ICD `cmd_bypass_delay` — ≥ 500 ms pulse |
| DO 4 | (spare) | — |
| DO 5 | (spare) | — |

**Do not** wire DO 4 or DO 5 to ATS terminals 14, 15, or 16 — those are
factory-use only.

### Digital-input read function code (FC01 vs FC02)

The driver reads the 6 DIs with a Modbus function code chosen by
`io.adam.di_read` (`coils` = FC01, the default, or `discrete_inputs` =
FC02). On the ADAM-6000 series the *documented* mapping for digital
inputs is **FC02 (read discrete inputs)** — FC01 (read coils) reads the
*relay outputs*. Some firmware also mirrors the DIs into the coil space,
so FC01 may work too. **This must be confirmed on the bench** (it's the
first thing `testadam.sh` exercises). If the live DI snapshot reads
all-`0` or `position` stays `unknown` no matter what the ATS is doing,
flip to `discrete_inputs` — no code change needed:

```bash
sudo ./testadam.sh --di-read discrete_inputs     # then set io.adam.di_read to match
```

The relay outputs (DOs) are always coils (FC01 read-back / FC05 write).

## 4. Network

- ADAM-6060: static IP, recommend `192.168.1.251`, on the OT VLAN
- Pi: static IP, recommend `192.168.1.250`, on the OT VLAN
- Both reachable by the GenWatch Pi (typically also on OT VLAN)
- Modbus TCP port 502 open between Pi and ADAM, and between Pi and
  GenWatch Pi
- NTP: both Pis sync against the same time source (router or one of
  the Pis serves NTP)

### 4.1 Network segmentation is a safety control (F3)

The ATS-Pi runs a Modbus TCP **server** on port 502, and Modbus/TCP has **no
authentication** by design. The server does restrict writes to the four ICD
command registers and enforces the `assumed_mode` policy, but anything that can
*route to* port 502 on the Pi can drive Test / Inhibit / Force-Transfer /
Bypass within that policy. For a deployment that *commands* a real switch, the
network boundary is therefore a **safety control** and must be a deliberate,
documented decision — not the incidental `0.0.0.0` dev default. Two layers:

1. **Bind to the OT interface.** Set `modbus_server.host` in
   `/etc/atspi/config.yaml` to the Pi's specific OT-VLAN IP (e.g.
   `192.168.1.250`), not `0.0.0.0`, so the server only listens on that segment.
2. **Firewall allowlist.** On a dedicated OT VLAN, allow only:
   - `GenWatch Pi → ats-pi:502` (inbound commands/reads), and
   - `ats-pi → ADAM:502` (outbound I/O).

   Block everything else to `ats-pi:502`. Example host firewall on the Pi
   (adapt to your tooling; `<genwatch-ip>` is the only permitted client):

   ```bash
   # Allow the GenWatch Pi to reach the Modbus server; drop all other :502.
   sudo iptables -A INPUT -p tcp --dport 502 -s <genwatch-ip> -j ACCEPT
   sudo iptables -A INPUT -p tcp --dport 502 -j DROP
   ```

   The host firewall is the enforcement layer because pymodbus has no
   connection-source ACL — keep it in the commissioning runbook, not just in
   someone's head.

**Acceptance:** from a host *off* the OT segment, `nmap -p 502 <ats-pi-ip>`
shows the port **filtered**; only the GenWatch Pi can reach it. Record this on
the commissioning sign-off alongside the F1 cable-pull.

## 5. Install sequence

Recommend doing this in a planned outage window, but the work itself
is non-invasive to ATS function (you're only landing wires on existing
customer terminals).

1. LOTO the ATS (utility AND generator sources)
2. Mount the PSU, ADAM, and Pi enclosure on DIN rail or sub-bracket
   inside or adjacent to the ATS cabinet
3. Land 120 VAC L/N/G to the PSU; verify 24 VDC out
4. Land DI wires from ATS contact terminals to ADAM DI channels per §3
5. Land DO wires from ADAM relay outputs to ATS input terminals per §3.
   **Verify ASCO terminal block labels** against operator's manual
   `381333-289` for your specific unit — pin numbering can vary by
   ATS catalog number
6. Wire Cat6 to both Pi and ADAM, through surge protectors, out to LAN
7. Remove LOTO, re-energize
8. Configure ADAM IP (it ships at `10.0.0.1` — use Advantech's utility)
9. **Configure the ADAM host watchdog / DO fail-safe** — see §5.1 below
10. Configure Pi network, install Raspbian, then this project per
    `docs/DEVELOPMENT.md`

### 5.1 Configure the ADAM host watchdog (critical hardware fail-safe)

> **Do not skip this.** It is the only thing that releases a maintained
> relay if the *Pi itself* dies. Configure it while you have the
> Advantech utility open in step 8.

The service has a software safety watchdog (`safety.py`, ICD §8.3): if
GenWatch stops polling, it auto-releases `cmd_inhibit` /
`cmd_force_transfer` after 30 s. That covers *"GenWatch went silent while
the Pi is alive."* It **cannot** cover *"the Pi lost power / kernel
panicked / was unplugged"* while Force Transfer or Inhibit is asserted —
once the software is gone, the ADAM latches the last commanded relay
state **indefinitely**, leaving the ATS forced to the generator (or
transfers blocked) with nothing left to release it.

The ADAM-6000 series has the hardware backstop for exactly this: a
**host idle (communication watchdog) timer** with a per-channel **Digital
Output Safety Value**. When the host stops talking Modbus for the
configured timeout, the ADAM drives each DO to its safe value on its own.

Configure it (Advantech ADAM/Apax .NET Utility, or the module's web UI):

| Setting | Value | Why |
|---|---|---|
| Host idle / comms watchdog | **Enabled** | Arms the fail-safe |
| Watchdog timeout | **5–10 s** | Longer than a sampling blip, shorter than the 30 s software watchdog so it only fires on true host loss |
| DO 0 (Test) safe value | **0 / OFF** | De-energise |
| DO 1 (Force Transfer) safe value | **0 / OFF** | Release the forced transfer |
| DO 2 (Inhibit) safe value | **0 / OFF** | Stop inhibiting |
| DO 3 (Bypass Delay) safe value | **0 / OFF** | De-energise |
| DO 4 / DO 5 | **0 / OFF** | Spares, keep de-energised |

After enabling it, **verify**: with the service stopped, assert Inhibit
once (`atspi-bench --skip-dis`, drive DO 2), then pull the Pi's network
cable and confirm the ADAM relay drops within the timeout (watch the
DO 2 LED on the module). Re-seat the cable when done. **Record the result
on the commissioning sign-off** — this cable-pull is the real acceptance
test for F1; the software check below cannot prove the relay physically
drops.

### 5.2 Software readback of the §5.1 fail-safe (F1)

> **ADAM-6060: this readback is NOT possible — keep
> `require_hw_watchdog: false` on that model.** Bench validation on the
> real module ([`BENCH.md §10`](./BENCH.md), finding 4) found the 6060
> does not expose its FSV / Communication-WDT / host-idle registers over
> Modbus at all (they are configurable only via the Windows .NET
> Utility), so this self-check would always fail closed and refuse
> outputs. On the 6060 the §5.1 **cable-pull test is the F1 acceptance
> gate** ([`NEXTSTEPS.md`](./NEXTSTEPS.md) Stage 2/3.2): record it on
> the sign-off, and **re-run it after any ADAM swap or factory reset** —
> with the software gate waived, nothing else will warn you the FSV is
> gone. The rest of this section applies to ADAM models/firmware that
> do expose the registers.

The §5.1 watchdog is configured by hand, and nothing used to confirm it
stayed configured — skip the step, or later factory-reset / swap the ADAM,
and the only backstop against a Pi crash stranding a latched relay was
silently gone. The service now **reads that config back on connect** and
refuses to drive the switch if it can't confirm the fail-safe is armed.

With `io.adam.require_hw_watchdog: true` (the default), on every connect the
driver reads the host-watchdog enable register, the timeout register, and the
per-DO safety-value registers, and treats the fail-safe as **armed** only if:

- the host watchdog is **enabled**,
- its timeout is inside the **5–10 s** band (§5.1), and
- **every** DO safety value is **0 / OFF**.

If any of those is false — or the registers aren't configured, or can't be
read — the check **fails closed**:

- the driver **refuses to assert** any output (Test / Inhibit / Force-Transfer
  / Bypass). A **release** is always allowed, so the comms-loss software
  watchdog and the bench cleanup can still drop relays.
- a persistent **`OUTPUT_FAULT`** is published, so GenWatch sees a
  non-authoritative ATS link and its authority gate refuses commands anyway.
- the reason is logged loudly at startup (`journalctl -u atspi`).

**You must supply the register addresses.** They live in the *ADAM-6000 Series
User Manual* (Appendix B, "Modbus/TCP addresses of ADAM-6000 modules") and
vary by model / firmware revision — the same bench-verify discipline as the DI
function-code toggle (§3). They are left unset by default, so until you fill
them in (and `require_hw_watchdog` is true) the gate stays closed. Set them in
`/etc/atspi/config.yaml` under `io.adam.hw_watchdog` (addresses are PDU
offsets, 0-based):

```yaml
io:
  adam:
    require_hw_watchdog: true
    hw_watchdog:
      enable_register: 0x????            # from the manual, then BENCH-VERIFY
      enable_expected: 1                 # value that reads back as "enabled"
      timeout_register: 0x????
      timeout_scale_s: 0.1               # seconds per raw count (BENCH-VERIFY)
      timeout_min_s: 5
      timeout_max_s: 10
      safety_value_register_base: 0x????
      safety_value_count: 6
```

The self-check reads these as **holding registers (FC03)**. Most ADAM-6000
firmware exposes the watchdog/safety config there; if yours maps them to the
coil space instead, that's a bench-verify finding to flag. To confirm the
addresses, read them back with the watchdog deliberately set both ways (e.g.
`modpoll -m tcp -t 4 -r <reg+1> -c 1 <adam-ip>` reads holding register
`<reg>`) and check the values track what you set in the Advantech utility —
that also tells you `enable_expected` and the right `timeout_scale_s` (read the
raw value at a known timeout). **Verify with the §5.1 cable-pull, not just the
readback**: a wrong address can coincidentally read an "armed-looking" value,
so the physical drop is what actually proves the fail-safe — record it on the
sign-off.

For bench work where you intend to drive outputs without the fail-safe (e.g. a
module on the desk), set `require_hw_watchdog: false` — an explicit, auditable
waiver. `atspi-bench` already waives it internally.

## 6. Verifying contact reads (before integrating with GenWatch)

From the Pi:

```bash
# Confirm the ADAM is reachable
ping -c 3 192.168.1.251
```

The `atspi-bench` command walks through every DI and DO interactively —
prompts you to actuate each contact, reads the ADAM, confirms the
correct bit changed, then drives each DO in turn and asks you to
confirm the matching ASCO terminal responded:

```bash
atspi-bench --host 192.168.1.251 --port 502 --unit-id 1
# add --skip-dos when the ATS is energised and a load flip is unsafe
# add --json to capture the per-step results to a file:
#   atspi-bench --host 192.168.1.251 --json > bench-results.json
```

Exit codes: 0 = all checks passed, 1 = at least one failed, 2 = ADAM
unreachable, 3 = skipped at least one check (incomplete).

For ad-hoc spot checks without the interactive flow (note the `-t` flag —
bare `modpoll` reads holding registers, not the DI bits):

```bash
# Read the six DIs as discrete inputs (FC02, -t 1) ...
modpoll -m tcp -a 1 -r 1 -c 6 -t 1 192.168.1.251
# ... or as coils (FC01, -t 0) if io.adam.di_read: coils works on your unit
modpoll -m tcp -a 1 -r 1 -c 6 -t 0 192.168.1.251
# Read the six relay outputs (always coils, FC01)
modpoll -m tcp -a 1 -r 17 -c 6 -t 0 192.168.1.251
```

Whichever DI function code shows the contacts changing is the value to
put in `io.adam.di_read` (see §3). `testadam.sh` does this for you.

Then physically:

- Press the front-panel "Test" momentarily on the ASCO → DI 0 should pulse
- Trip the utility breaker upstream (briefly!) → DI 3 should drop
- Disable the generator → DI 4 should drop, DI 5 should go high

Once all six inputs respond correctly, install the `atspi` service
(see `docs/DEVELOPMENT.md`) and proceed with end-to-end testing
against GenWatch.

## 7. Commissioning checklist (Pi side, after wiring)

Once §5 (install sequence) and §6 (verify reads) are done, follow this
checklist in order. Each step has an "if this fails" pointer so an
operator can self-rescue without paging an SRE.

**The quick path** — two scripts in the repo root automate most of this:

```bash
sudo ./install.sh     # service user + venv + /etc/atspi/config.yaml + systemd unit
sudo nano /etc/atspi/config.yaml   # driver: adam, io.adam.host, site.unit_id, di_read
# Configure the ADAM host watchdog / DO fail-safe now (§5.1) — don't skip it.
sudo ./testadam.sh    # ping + live Modbus snapshot + interactive atspi-bench
sudo systemctl enable --now atspi  # only after testadam.sh passes
```

If `testadam.sh`'s DI snapshot is all-`0` while the ATS is clearly on a
source, re-run it with `--di-read discrete_inputs` and set
`io.adam.di_read` to match (§3).

`install.sh` installs into a venv at `/opt/atspi/venv` and points the unit's
`ExecStart` there, so the `status=127` gotcha below doesn't apply to a scripted
install. It's idempotent (re-run it after a `git pull` to upgrade) and never
overwrites an existing config. The manual steps below are the reference for
what those scripts do and for troubleshooting.

```bash
# 1. Confirm the package is on the path. Falls under DEVELOPMENT.md
#    "Production install"; if the venv vs system-wide split matters
#    here you'll see "command not found" and need to point
#    systemd/atspi.service ExecStart at the right path.
which atspi
atspi --version

# 2. Create the service user BEFORE first systemctl start.
#    Systemd will fail-fast with "User/Group resolution: 'atspi' not
#    found" if you skip this — visible in journalctl, not stdout.
id atspi || sudo useradd --system --no-create-home --shell /usr/sbin/nologin atspi

# 3. Install config; FLIP THE DRIVER from mock to adam.
#    With driver: mock the service reads from RAM and reports a
#    constant healthy snapshot — easy to miss because it 'works'.
sudo mkdir -p /etc/atspi
sudo cp config.example.yaml /etc/atspi/config.yaml
sudo sed -i 's/^  driver: mock/  driver: adam/' /etc/atspi/config.yaml
# also: edit io.adam.host, site.unit_id to site values
sudo nano /etc/atspi/config.yaml

# 4. Bench-verify EVERY channel before enabling the service.
#    The bench tool exits 0 only when every step passed.
atspi-bench --host 192.168.1.251 --port 502 --unit-id 1

# 5. Install + start the systemd unit. StateDirectory= auto-creates
#    /var/lib/atspi with the right perms; no chown needed.
sudo cp systemd/atspi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now atspi
sudo systemctl status atspi   # should show 'active (running)'

# 6. End-to-end smoke from another host on the OT VLAN.
#    Should NOT match the mock's hardcoded [0,1,1,0,0,0] snapshot —
#    if it does, you forgot step 3.
modpoll -m tcp -a 1 -r 1 -c 6 <pi-ip>

# 7. Verify the unit_id register matches GenWatch's expected_unit_id.
#    A mismatch makes GenWatch refuse to connect (silent on the wire).
modpoll -m tcp -a 1 -r 54 -c 1 <pi-ip>   # 0x0035 = site.unit_id

# 8. Enable GenWatch's ATS consumer (ats.enabled: true in its config)
#    and confirm the dashboard ATS card lights up within ~2 prime
#    polls (3 s default).
```

If any of steps 4-7 fail, see `docs/RUNBOOK.md`. Common gotchas:

| Symptom | Likely cause | Fix |
|---|---|---|
| `systemctl status` → `code=exited, status=127` | `/usr/local/bin/atspi` doesn't exist (venv install) | Point `ExecStart=` at the venv's atspi binary |
| `User/Group resolution: 'atspi' not found` | Skipped step 2 | `useradd` command above |
| modpoll returns `[0, 1, 1, 0, 0, 0]` no matter what the ATS is doing | `driver: mock` still in config | Step 3 sed/nano |
| All DIs read `0` / `position` stuck `unknown` / every `atspi-bench` DI step says "no bit change" | Wrong DI function code | Set `io.adam.di_read: discrete_inputs` (§3); confirm with `testadam.sh --di-read discrete_inputs` |
| modpoll times out | Firewall on Pi (`iptables -L`) or wrong bind (`modbus_server.host`) | Allow GenWatch→502 in / Pi→ADAM:502 out (§4.1); confirm `modbus_server.host` is the OT-VLAN IP the client can reach |
| Journal shows `sampling cycle failed (OSError): ADAM read_coils ... failed` then `sampling still failing` reminders | ADAM unreachable or wrong IP | `ping <io.adam.host>` from Pi; check Cat6 |

## 8. Safety reminders

- ATS internals are at 480 V / 600 A. All work inside the cabinet
  requires a qualified electrician with proper PPE and LOTO procedures
  per NFPA 70E.
- The ADAM-6060's relay contacts are dry — they don't backfeed voltage,
  but the ASCO inputs they drive are tied to controller-internal logic
  voltages. Maintain proper isolation when troubleshooting.
- Never use the spare DO channels (DO 4, DO 5) without re-verifying
  the ASCO terminal documentation — terminals 14-16 are factory-use
  and writing to them may cause damage.
- The software safety watchdog only protects you while the Pi is alive.
  The ADAM host watchdog / DO fail-safe (§5.1) is what releases a
  maintained relay if the Pi loses power or crashes — configure and
  verify it before the switch is ever driven in anger.

## 9. Commission a pinned, tagged build (F4)

The 221+ unit tests prove the code is internally consistent **against a
simulation** — not that the ADAM coil bases / read function code are right for
*your* module, or that the ASCO terminal mapping matches *your* switch's
catalog number. Those are bench-verified (§3, §5.2, §6), and a facility should
never run off a moving `main`. So:

- **Tag the exact commit you commission against**, e.g. `v0.1.0-rc1`, and only
  ever deploy that tag to the plant (`git checkout v0.1.0-rc1` before
  `install.sh`). `atspi --version` then matches the sign-off sheet.
- **Commission it together with the matching GenWatch tag** — work the shared
  `COMMISSIONING.md` hardware-in-the-loop procedure (read side first, commands
  last, in a planned outage).
- **Record on the sign-off sheet:** the deployed tag, the F1 §5.1 cable-pull
  result, the §5.2 watchdog-readback config (the verified register addresses),
  and the F3 §4.1 `nmap` filtered check.

Acceptance for go-live: a tagged build, referenced in a signed-off commissioning
sheet, with F1 verified on the real unit.
