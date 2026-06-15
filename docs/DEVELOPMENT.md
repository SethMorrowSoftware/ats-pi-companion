# Development

For commissioning a real ATS-Pi (Pi + ADAM + ASCO), see
[`HARDWARE.md §7`](./HARDWARE.md). For field troubleshooting, see
[`RUNBOOK.md`](./RUNBOOK.md). This document is for someone hacking
on the service itself.

## Prerequisites

- Python **3.11+**
- A POSIX shell
- `pip` and `venv`
- For ADAM testing: an ADAM-6060 (or any Modbus TCP I/O device with
  ≥ 6 DI + 6 DO) on the same LAN
- For the **hybrid** path: also a USB-RS485 adapter plus an ASCO Group 5
  controller — or any serial Modbus RTU slave simulator on the adapter's
  `/dev/ttyUSB*` node — to exercise the serial monitoring read

## Setup

```bash
git clone https://github.com/SethMorrowSoftware/ats-pi-companion.git
cd ats-pi-companion

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

The dev extra pulls in `pytest`, `pytest-asyncio`, and `ruff`. The
`pymodbus` runtime dep is pinned to `>=3.7.4,<3.8` — bumping past 3.7
requires porting the datastore code (see `CHANGELOG.md` for rationale).

## Running with the mock driver

```bash
cp config.example.yaml config.yaml      # defaults to driver: mock
atspi --config config.yaml --log-level INFO
```

In another terminal:

```bash
modpoll -m tcp -a 1 -r 1 -c 6 127.0.0.1
# → [0, 1, 1, 0, 0, 0]  default healthy state
```

### Driving the mock at runtime

The mock driver installs two signal handlers when running on an event
loop:

| Signal | Effect |
|---|---|
| `SIGUSR1` | Cycles `position` through `utility → generator → transferring → unknown → utility` |
| `SIGUSR2` | Toggles `normal_available`; `engine_start_calling` mirrors the inverted value (as the real ASCO behaves) |

```bash
kill -USR1 $(pgrep -f 'atspi --config')   # advance position
kill -USR2 $(pgrep -f 'atspi --config')   # drop / restore utility
```

This is enough to drive an end-to-end test against a running GenWatch
without recompiling. For richer scenarios, write a pytest that
instantiates `IOMockDriver` directly and calls `set_position()` /
`set_normal_available()` — see `tests/test_state.py` for the pattern.

## Running tests

```bash
python -m pytest tests/ -v
python -m ruff check src/ tests/
```

The suite (currently 266 tests) runs entirely without hardware — mock
driver for the I/O layer, a fake pymodbus client for the ADAM and hybrid
drivers (`test_io_adam.py`, `test_io_hybrid.py`), `tmp_path` for
persistence. CI runs both jobs on every push plus a
`soak` job that starts the real binary, performs a real Modbus read,
and asserts a clean SIGTERM exit; and an `audit` job (`pip-audit`,
informational).

The contract tests in `tests/test_icd_contract.py` drive a real
pymodbus client against the real server in-process. They are the
canonical defence against ICD drift on the ATS-Pi side.

## Manual integration testing against GenWatch

1. Start the ATS-Pi with mock I/O on its default port (5020 — the
   non-root service can't bind the privileged 502).
2. In GenWatch's `/etc/genwatch/config.yaml`:

   ```yaml
   ats:
     enabled: true
     host: <ats-pi-ip>
     port: 5020
     expected_unit_id: 23     # must match this project's site.unit_id
   ```

   Restart GenWatch.

3. GenWatch's ATS card should populate with `position: utility, both
   sources available`. The `loadSource` indicator should annotate
   "(via ATS-Pi)".

4. Drive transitions: `kill -USR1` cycles position; `kill -USR2` drops
   utility. GenWatch's events feed and load source should follow.

5. **Test the safety auto-release.** From GenWatch, assert Inhibit.
   Stop GenWatch (`sudo systemctl stop genwatch`). Wait 35 s.
   `modpoll -m tcp -a 1 -r 66 -c 1 <ats-pi-ip>` (PDU `0x0041`) should
   read `0` — the watchdog has released.

## Debugging

```bash
# Watch live logs
sudo journalctl -u atspi -f

# Modbus reachability
modpoll -m tcp -a 1 -r 1 -c 6 <ats-pi-ip>

# ICD version
modpoll -m tcp -a 1 -r 49 -c 2 <ats-pi-ip>   # 0x0030, 0x0031

# Full ICD register block
modpoll -m tcp -a 1 -r 1 -c 80 <ats-pi-ip>
```

`modpoll` isn't in Raspbian's default package set — `sudo apt install
modbus-cli` once per Pi.

## Contributing back

The ICD is the source of truth. If you find a contract issue:

1. **Don't** silently work around it in either project.
2. Open a PR against the GenWatch repo's
   `docs/integrations/ats-pi-icd.md` describing the proposed change,
   with rationale and the version bump (minor for additive, major for
   breaking).
3. Once that merges, update this project to match in a follow-up PR.

Keeping the ICD authoritative is what lets the two projects evolve
without breaking each other.
