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
│  ATS        │  18RX,     │   6 relay   │  192.168.   │  (this proj)│  port 502   │ dashboard│
│  Group 5    │  14AA/14BA │   DO        │  x.251      │             │             │          │
└─────────────┘  test/     └─────────────┘             └─────────────┘             └──────────┘
                 inhibit/
                 transfer
```

## Scope

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
(44 tests against a real pymodbus client driving the real server).
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
modpoll -m tcp -a 1 -r 1 -c 6 127.0.0.1
# → [0, 1, 1, 0, 0, 0]  (utility, both sources available, auto, no faults)
```

The mock driver responds to `SIGUSR1` (cycles position
`utility → generator → transferring → unknown`) and `SIGUSR2` (toggles
`normal_available` + mirrored `engine_start_calling`), so you can drive
a running service through state transitions without recompiling.

## Production deployment

The literal command sequence from "Pi configured" to "GenWatch sees
the ATS" is in [`docs/HARDWARE.md §7`](./docs/HARDWARE.md), including
the five most likely first-boot gotchas. If something goes sideways
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
  bench.py           `atspi-bench` interactive commissioning CLI
  persistence.py     atomic-rename JSON state file
  health.py          optional localhost JSON /health endpoint
  notify.py          sd_notify (systemd Type=notify)

docs/
  SPEC.md            implementation architecture
  HARDWARE.md        BOM, wiring, install, commissioning checklist
  DEVELOPMENT.md     dev environment, mock controls, test layout
  RUNBOOK.md         field troubleshooting

tests/               197 tests, ruff-clean, CI on every PR
systemd/atspi.service production unit (Type=notify, WatchdogSec=60)
```

## License

MIT — matches GenWatch.
