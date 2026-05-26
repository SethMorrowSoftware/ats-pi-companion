# Development

## Prerequisites

- Python **3.11+**
- A POSIX shell
- `pip` and `venv`
- (For hardware testing) an ADAM-6060 or any other Modbus TCP I/O device
  with at least 6 DI + 6 DO channels

## Setup

```bash
git clone https://github.com/SethMorrowSoftware/ats-pi-companion.git
cd ats-pi-companion

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running with mock I/O (no hardware)

The `IOMockDriver` keeps all contact states in memory. Start the server,
then drive states interactively:

```bash
cp config.example.yaml config.yaml
# In config.yaml, set io.driver: mock
atspi --config config.yaml --log-level INFO
```

In another terminal, verify reads:

```bash
pip install modbus-cli   # if not already
modpoll -m tcp -a 1 -r 1 -c 6 127.0.0.1

# Should return six register values matching the default
# healthy state: [0, 1, 1, 0, 0, 0]
# (position=0 utility, normal=1, emergency=1, eng_start=0, mode=0 auto, fault=0)
```

To flip mock state from a Python shell:

```python
# In a third terminal
import requests   # if we add a small HTTP control endpoint to the mock
# OR directly:
from atspi.io_mock import mock_global_state  # convention TBD
mock_global_state.set_normal_available(False)
```

(The mock driver should expose a simple control interface — REPL, HTTP
endpoint on localhost, or signal handlers. Choose what's easiest for
the implementer; document the choice.)

## Running tests

```bash
python -m pytest tests/ -v
```

The test suite (51 tests) runs entirely without hardware — uses the
mock driver, an in-memory fake for the ADAM driver, and tmp files for
persistence. Lint with:

```bash
python -m ruff check src/ tests/
```

CI runs both on every push (see `.github/workflows/ci.yml`).

## Manual integration testing against GenWatch

1. Start the ATS-Pi server with mock I/O as above on its standard port
   (502, or 5020 in dev to avoid root requirement).
2. On the GenWatch dev machine, set in `/etc/genwatch/config.yaml`:

   ```yaml
   ats:
     enabled: true
     host: <ats-pi-ip>
     port: 5020
   ```

   Restart GenWatch (`sudo systemctl restart genwatch` or your dev
   equivalent).

3. In GenWatch's UI:
   - The ATS card should populate with `position: utility, both sources
     available`.
   - The `loadSource` indicator should annotate "(via ATS-Pi)".

4. Drive state changes via the ATS-Pi's mock control interface:
   - Set `normal_available=False` → GenWatch's events feed should
     log a `UTILITY_LOST` event.
   - Set `position=generator` → GenWatch's loadSource should flip to
     `GENERATOR`.

5. Test the safety auto-release:
   - From GenWatch, issue an Inhibit command (when Phase 3 of the
     GenWatch plan is live).
   - Stop GenWatch (`sudo systemctl stop genwatch`).
   - Wait 35 s.
   - Confirm `cmd_inhibit_active` in the ATS-Pi's register reads `0`
     (you can verify with `modpoll`).

## Production install

```bash
sudo cp systemd/atspi.service /etc/systemd/system/
sudo cp config.example.yaml /etc/atspi/config.yaml
# Edit /etc/atspi/config.yaml for the site
sudo systemctl enable atspi
sudo systemctl start atspi
sudo journalctl -u atspi -f
```

The service starts on boot, restarts on crash, and logs to journal.

## Debugging

```bash
# Watch live logs
sudo journalctl -u atspi -f

# Check Modbus reachability
modpoll -m tcp -a 1 -r 1 -c 6 <ats-pi-ip>

# Check ICD version
modpoll -m tcp -a 1 -r 49 -c 2 <ats-pi-ip>   # 0x0030, 0x0031

# Dump every register the spec defines
modpoll -m tcp -a 1 -r 1 -c 80 <ats-pi-ip>   # raw read of full block
```

## Contributing back

The ICD is the source of truth. If you discover a contract issue:

1. **Don't** silently work around it in either project.
2. Open a PR against the GenWatch repo's `docs/integrations/ats-pi-icd.md`
   describing the proposed change. Include rationale and the version
   bump (minor for additive, major for breaking).
3. Once that PR is merged, update this project to match in a follow-up PR.

Keeping the ICD authoritative is what lets the two projects evolve
without breaking each other.
