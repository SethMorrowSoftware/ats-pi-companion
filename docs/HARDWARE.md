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

## 4. Network

- ADAM-6060: static IP, recommend `192.168.1.251`, on the OT VLAN
- Pi: static IP, recommend `192.168.1.250`, on the OT VLAN
- Both reachable by the GenWatch Pi (typically also on OT VLAN)
- Modbus TCP port 502 open between Pi and ADAM, and between Pi and
  GenWatch Pi
- NTP: both Pis sync against the same time source (router or one of
  the Pis serves NTP)

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
9. Configure Pi network, install Raspbian, then this project per
   `docs/DEVELOPMENT.md`

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

For ad-hoc spot checks without the interactive flow:

```bash
# Read all six DIs as a packed register
modpoll -m tcp -a 1 -r 1 -c 1 192.168.1.251
```

Then physically:

- Press the front-panel "Test" momentarily on the ASCO → DI 0 should pulse
- Trip the utility breaker upstream (briefly!) → DI 3 should drop
- Disable the generator → DI 4 should drop, DI 5 should go high

Once all six inputs respond correctly, install the `atspi` service
(see `docs/DEVELOPMENT.md`) and proceed with end-to-end testing
against GenWatch.

## 7. Safety reminders

- ATS internals are at 480 V / 600 A. All work inside the cabinet
  requires a qualified electrician with proper PPE and LOTO procedures
  per NFPA 70E.
- The ADAM-6060's relay contacts are dry — they don't backfeed voltage,
  but the ASCO inputs they drive are tied to controller-internal logic
  voltages. Maintain proper isolation when troubleshooting.
- Never use the spare DO channels (DO 4, DO 5) without re-verifying
  the ASCO terminal documentation — terminals 14-16 are factory-use
  and writing to them may cause damage.
