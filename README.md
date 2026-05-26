# ATS-Pi Companion

Dedicated Raspberry Pi service that physically observes an ASCO Series 300
Power Transfer Switch and exposes its state to the GenWatch dashboard
over Modbus TCP.

```
        ASCO 300 ATS                          GenWatch Pi
   (Group 5 controller)                    (generator monitor)
            │                                       ▲
            │ dry contacts                          │ Modbus TCP
            │ (18RX, 14AA/14BA,                     │ (this project
            │  test/inhibit/transfer)               │  is the server)
            ▼                                       │
       ┌──────────┐                                 │
       │ ATS-Pi   │ ── Modbus TCP (port 502) ──────▶│
       │ (this)   │
       └──────────┘
```

## What this project does

- Reads the ASCO's dry contacts: source availability (Normal / Emergency),
  switch position (On Normal / On Emergency), engine-start sense
- Exposes these as Modbus TCP holding registers per the ICD
- Accepts write commands from GenWatch (Test, Inhibit, Force Transfer,
  Bypass Delay) and drives the corresponding ASCO inputs with correct
  pulse timing and safety auto-release
- Reports its own health (input/output faults, ICD version, uptime)

## What this project does NOT do

- It does not provide its own UI. All operator-visible state and
  commands live in GenWatch.
- It does not directly observe the generator (that's the H-100 → GenWatch
  path).
- It does not implement any building-side energy metering. If a meter
  is added later, the ICD can be extended (minor-version bump).

## Interface contract

The wire protocol and semantic contract are **frozen** in the
**ICD document**, which lives in the GenWatch repo:

→ [`docs/integrations/ats-pi-icd.md`](https://github.com/SethMorrowSoftware/GenWatch/blob/main/docs/integrations/ats-pi-icd.md)

You MUST read this before implementing any of the server. Every
register address, encoding, and timing requirement is specified there.

## Project layout

```
src/atspi/
  __init__.py       — package, version
  __main__.py       — CLI entry: `python -m atspi --config ...`
  config.py         — YAML config loader
  server.py         — Modbus TCP server, mounts the register store
  state.py          — internal state model (mirrors ICD §5 register layout)
  safety.py         — 30-second comms-loss auto-release per ICD §8.3
  io_driver.py      — abstract I/O base class
  io_mock.py        — mock driver for dev/testing without hardware
  io_adam.py        — Advantech ADAM-6060 driver
  persistence.py    — atomic JSON state file for lifetime counters
  notify.py         — sd_notify integration (systemd Type=notify)

docs/
  SPEC.md           — implementation specification (companion to the ICD)
  HARDWARE.md       — BOM, wiring, install
  DEVELOPMENT.md    — getting started, running tests, manual testing

tests/
  test_smoke.py     — imports, basic config load
  test_state.py     — register store + transitions + persistence
  test_safety.py    — comms-loss auto-release
  test_io_adam.py   — ADAM driver bit decoding (against a fake client)
  test_server.py    — data block routing + command dispatch
  test_persistence.py — atomic write, corruption recovery
  test_notify.py    — sd_notify socket protocol

systemd/
  atspi.service     — production systemd unit (Type=notify, watchdog)
```

## Quick start (dev)

```bash
git clone https://github.com/SethMorrowSoftware/ats-pi-companion.git
cd ats-pi-companion
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run with the mock I/O driver — no hardware required
cp config.example.yaml config.yaml
atspi --config config.yaml

# In another terminal, test reads against it:
modpoll -m tcp -a 1 -r 1 -c 6 127.0.0.1
```

## Status

**Production-ready in software; bench verification of the ADAM-6060
driver register map is the only remaining task before site install.**

What's done:

- Register store, sampling loop, Modbus TCP server, command dispatch
- Safety watchdog (ICD §8.3 30 s comms-loss auto-release)
- ADAM-6060 driver — implemented against the documented register map,
  needs bench verification per `docs/SPEC.md §8 Phase E`
- Persistence for `transfer_count_lifetime` (atomic JSON file)
- 24h sliding-window transfer counter
- systemd `Type=notify` with watchdog ping (60s)
- 51 unit tests, ruff-clean, CI on every PR

The companion **GenWatch consumer** for this service is already
shipped (`ats.enabled: true` in GenWatch's config). It will fall back
to H-100-derived loadSource until this project starts responding on
its configured host/port.

## License

MIT (matches GenWatch).
