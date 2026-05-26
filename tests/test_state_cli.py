"""Tests for the --export-state / --import-state CLI flags."""
from __future__ import annotations

import json
from pathlib import Path

from atspi.__main__ import _export_state, _import_state
from atspi.persistence import PersistedState, StateFile


def _write_config(tmp_path: Path, state_file: Path) -> Path:
    """Write a minimal valid config.yaml pointing at state_file."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"modbus_server:\n  port: 5020\n"
        f"site:\n  unit_id: 23\n"
        f"persistence:\n  state_file: {state_file}\n"
    )
    return cfg


def test_export_state_round_trips(tmp_path):
    """An exported file can be re-imported with no loss."""
    live = tmp_path / "state.json"
    StateFile(live).save(PersistedState(
        transfer_count_lifetime=42,
        last_transfer_to_gen_ts=1_700_000_000,
        last_retransfer_to_util_ts=1_700_001_000,
    ))
    cfg = _write_config(tmp_path, live)

    backup = tmp_path / "backup.json"
    rc = _export_state(str(cfg), str(backup))
    assert rc == 0
    assert backup.exists()

    # The backup file is a valid PersistedState JSON.
    loaded = StateFile(backup).load()
    assert loaded.transfer_count_lifetime == 42
    assert loaded.last_transfer_to_gen_ts == 1_700_000_000


def test_export_state_handles_missing_source(tmp_path):
    """No state.json yet → export still produces a file (zeros)."""
    live = tmp_path / "state.json"  # not created
    cfg = _write_config(tmp_path, live)
    backup = tmp_path / "backup.json"
    rc = _export_state(str(cfg), str(backup))
    assert rc == 0
    loaded = StateFile(backup).load()
    assert loaded.transfer_count_lifetime == 0


def test_import_state_writes_to_configured_location(tmp_path):
    live = tmp_path / "state.json"
    cfg = _write_config(tmp_path, live)

    source = tmp_path / "from-other-pi.json"
    StateFile(source).save(PersistedState(transfer_count_lifetime=777))

    rc = _import_state(str(cfg), str(source))
    assert rc == 0
    assert live.exists()
    assert StateFile(live).load().transfer_count_lifetime == 777


def test_import_state_refuses_missing_source(tmp_path, capsys):
    cfg = _write_config(tmp_path, tmp_path / "state.json")
    rc = _import_state(str(cfg), str(tmp_path / "nope.json"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "source file not found" in err


def test_import_state_refuses_corrupt_json(tmp_path, capsys):
    """An explicit import of a corrupt file must NOT silently zero the
    live state — different policy from the on-boot tolerant load.
    """
    live = tmp_path / "state.json"
    StateFile(live).save(PersistedState(transfer_count_lifetime=999))  # protect this
    cfg = _write_config(tmp_path, live)

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not valid json")
    rc = _import_state(str(cfg), str(corrupt))
    assert rc == 2
    err = capsys.readouterr().err
    assert "not valid JSON" in err

    # Live state untouched.
    assert StateFile(live).load().transfer_count_lifetime == 999


def test_import_state_round_trip_via_export(tmp_path):
    """End-to-end: produce a backup with --export-state, then import it
    into a fresh location.
    """
    # Site A: write state, export.
    live_a = tmp_path / "siteA-state.json"
    StateFile(live_a).save(PersistedState(transfer_count_lifetime=123))
    cfg_a = tmp_path / "siteA.yaml"
    cfg_a.write_text(f"persistence:\n  state_file: {live_a}\n")
    backup = tmp_path / "backup.json"
    assert _export_state(str(cfg_a), str(backup)) == 0

    # Site B (e.g. spare Pi being cloned): import the same backup.
    live_b = tmp_path / "siteB-state.json"
    cfg_b = tmp_path / "siteB.yaml"
    cfg_b.write_text(f"persistence:\n  state_file: {live_b}\n")
    assert _import_state(str(cfg_b), str(backup)) == 0
    assert StateFile(live_b).load().transfer_count_lifetime == 123


def test_import_state_rejects_truncated_json(tmp_path):
    """An empty file is not valid JSON either."""
    cfg = _write_config(tmp_path, tmp_path / "state.json")
    empty = tmp_path / "empty.json"
    empty.write_text("")  # zero-byte file → JSONDecodeError
    rc = _import_state(str(cfg), str(empty))
    assert rc == 2


def test_import_state_message_warns_about_running_service(tmp_path, capsys):
    live = tmp_path / "state.json"
    cfg = _write_config(tmp_path, live)
    source = tmp_path / "src.json"
    source.write_text(json.dumps({"transfer_count_lifetime": 5}))
    assert _import_state(str(cfg), str(source)) == 0
    err = capsys.readouterr().err
    assert "restart" in err.lower()
