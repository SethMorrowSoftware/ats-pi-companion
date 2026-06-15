# ATS-Pi Companion

> Companion service to **[GenWatch](https://github.com/SethMorrowSoftware/GenWatch)**.
> Reads an **Advantech ADAM-6060** digital-I/O module wired to an
> **ASCO Series 300** automatic transfer switch and publishes the
> ICD-shaped state to GenWatch over Modbus TCP. Implements
> [`ats-pi-icd.md`](https://github.com/SethMorrowSoftware/GenWatch/blob/main/docs/integrations/ats-pi-icd.md)
> v1.0.

A dedicated Raspberry Pi service. The ADAM-6060 (6 digital inputs +
6 relay outputs, Modbus TCP) is the electrical bridge between the
ASCO's dry contacts and the network: its DIs sense the ATS state
(source availability, switch position, engine-start), its relay DOs
drive the ATS's command inputs (test, inhibit, force-transfer, bypass-
delay). This service polls the ADAM at 10 Hz, applies the ICD's
safety rules (mode-policy enforcement, 30-second comms-loss auto-
release, stuck-relay detection), and serves a Modbus TCP register
block to GenWatch.

```
┌─────────────┐   dry      ┌─────────────┐   Modbus    ┌─────────────┐   Modbus    ┌──────────┐
│  ASCO 300   │  contacts  │  ADAM-6060  │    TCP      │   ATS-Pi    │    TCP      │ GenWatch │
│  Series 300 │ ◀────────▶ │   6 DI +    │ ◀─────────▶ │   service   │ ◀─────────▶ │  Pi      │
│  ATS        │  18RX,     │   6 relay   │  192.168.   │  (this proj)│  port 5020  │ dashboard│
│  Group 5    │  14AA/14BA │   DO        │  x.251      │             │             │          │
└─────────────┘  test/     └─────────────┘             └─────────────┘             └──────────┘
                 inhibit/
                 transfer
```

## Scope

Two production I/O paths read the same ATS state (the ADAM always drives
the four command relays either way): `driver: adam` is contact-only and
needs the **18RX + 14AA/14BA** accessories so the ADAM's DIs can sense
position and source availability; `driver: hybrid` instead reads those
facts from the **ASCO Group 5 controller over RS-485** via a USB-RS485
adapter, so **no 18RX/aux accessories are needed** and the ADAM DIs go
unused. See [`docs/HARDWARE.md §3.1`](./docs/HARDWARE.md) to choose.

**This project does:**

- Read the ASCO's dry contacts (source availability, switch position,
  engine-start sense) through the ADAM-6060
- Publish them as Modbus TCP holding registers per the ICD
- Accept ICD command writes (Test, Inhibit, Force Transfer, Bypass
  Delay) with mode-policy enforcement and ICD-conformant pulse timing
- Auto-release maintained commands after 30 ± 5 s of comms silence
  (ICD §8.3)
- Report its own health (input/output faults, ICD version, uptime,
  lifetime / 24h transfer counts)

**This project does NOT:**

- Have its own UI — all operator-visible state and controls live in
  GenWatch
- Observe the generator directly — that's the H-100 → GenWatch path
- Implement building-side metering — ICD can extend later (minor bump)

## Interface contract

The ICD is the source of truth for every register address, encoding,
write semantics, and safety guarantee:

→ [`GenWatch/docs/integrations/ats-pi-icd.md`](https://github.com/SethMorrowSoftware/GenWatch/blob/main/docs/integrations/ats-pi-icd.md)

Conformance is verified end-to-end in `tests/test_icd_contract.py`
(47 tests against a real pymodbus client driving the real server).
Two documented deviations (Modbus exception codes 0x02 vs ICD's
0x03/0x04) are explained in `CHANGELOG.md`.

## Quick start (no hardware)

```bash
git clone https://github.com/SethMorrowSoftware/ats-pi-companion.git
cd ats-pi-companion
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp config.example.yaml config.yaml      # defaults to driver: mock
atspi --config config.yaml

# In another shell:
modpoll -m tcp -p 5020 -a 1 -r 1 -c 6 127.0.0.1
# → [0, 1, 1, 0, 0, 0]  (utility, both sources available, auto, no faults)
```

The mock driver responds to `SIGUSR1` (cycles position
`utility → generator → transferring → unknown`) and `SIGUSR2` (toggles
`normal_available` + mirrored `engine_start_calling`), so you can drive
a running service through state transitions without recompiling.

## Production deployment

**New to this? Start with [`docs/TUTORIAL.md`](./docs/TUTORIAL.md)** — a single
linear walkthrough of the whole job, from parts on the bench through wiring the
ASCO to seeing state in GenWatch, in the order it must be done. The summary
below is the quick reference; the tutorial stitches the deep docs together.

For the cabinet visit, hand the electrician
[`docs/FIELD-INSTALL.md`](./docs/FIELD-INSTALL.md) (a print-and-follow wiring
card), pre-stage [`config.production.example.yaml`](./config.production.example.yaml),
and record acceptance on [`docs/SIGN-OFF.md`](./docs/SIGN-OFF.md).

Two helper scripts wrap the commissioning sequence:

```bash
sudo ./install.sh     # service user + venv + config + systemd unit (doesn't start it)
# edit /etc/atspi/config.yaml: driver: adam, io.adam.host, site.unit_id
sudo ./testadam.sh    # ping + live Modbus snapshot + interactive atspi-bench wiring check
sudo systemctl enable --now atspi
```

`install.sh` is idempotent and never clobbers an existing config; it
deliberately leaves the service stopped so you bench-verify the ADAM
*before* the switch is ever driven. The literal step-by-step (and the
five most likely first-boot gotchas) is in
[`docs/HARDWARE.md §7`](./docs/HARDWARE.md). To validate a bare
ADAM-6060 on the bench first — before it is anywhere near the switch —
work [`docs/BENCH.md`](./docs/BENCH.md). If something goes sideways
at 2am, [`docs/RUNBOOK.md`](./docs/RUNBOOK.md) is the field guide.

## Project layout

```
src/atspi/
  __init__.py        package + ICD_VERSION = (1, 0)
  __main__.py        CLI entry; orchestrates sampling, server, watchdog
  config.py          strict YAML loader (rejects unknown keys)
  server.py          Modbus TCP server (pymodbus 3.7.x)
  state.py           register store — ICD §5 register layout
  safety.py          30-s comms-loss auto-release (ICD §8.3)
  io_driver.py       abstract I/O Protocol
  io_mock.py         dev/test driver with SIGUSR1/2 controls
  io_adam.py         Advantech ADAM-6060 driver
  io_asco_serial.py  ASCO Group 5 reader over RS-485 Modbus RTU (monitoring)
  io_hybrid.py       serial monitoring + ADAM control (no 18RX/aux contacts)
  bench.py           `atspi-bench` interactive commissioning CLI
  persistence.py     atomic-rename JSON state file
  health.py          optional localhost JSON /health endpoint
  notify.py          sd_notify (systemd Type=notify)

docs/
  TUTORIAL.md        start here — linear parts → ASCO → GenWatch walkthrough
  FIELD-INSTALL.md   one-page wiring card for the installing electrician
  SIGN-OFF.md        fill-in commissioning acceptance sheet
  SPEC.md            implementation architecture
  HARDWARE.md        BOM, wiring, install, commissioning checklist
  BENCH.md           bare-module bench bring-up + validation (pre-ASCO)
  NEXTSTEPS.md       staging verification (.NET config, GenWatch, gates)
  DEVELOPMENT.md     dev environment, mock controls, test layout
  RUNBOOK.md         field troubleshooting

tests/               266 tests, ruff-clean, CI on every PR
systemd/atspi.service production unit (Type=notify, WatchdogSec=60)
```

## License

MIT — matches GenWatch.
