"""Persistence module — atomic write, missing-file, and corruption tests."""
from __future__ import annotations

import json

from atspi.persistence import PersistedState, StateFile


def test_load_missing_file_returns_zeros(tmp_path):
    sf = StateFile(tmp_path / "state.json")
    s = sf.load()
    assert s.transfer_count_lifetime == 0
    assert s.last_transfer_to_gen_ts == 0
    assert s.last_retransfer_to_util_ts == 0


def test_save_and_load_round_trip(tmp_path):
    sf = StateFile(tmp_path / "state.json")
    sf.save(PersistedState(
        transfer_count_lifetime=42,
        last_transfer_to_gen_ts=1_700_000_000,
        last_retransfer_to_util_ts=1_700_001_000,
    ))
    loaded = sf.load()
    assert loaded.transfer_count_lifetime == 42
    assert loaded.last_transfer_to_gen_ts == 1_700_000_000
    assert loaded.last_retransfer_to_util_ts == 1_700_001_000


def test_load_corrupt_json_falls_back(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not valid json")
    sf = StateFile(p)
    s = sf.load()
    assert s.transfer_count_lifetime == 0


def test_load_wrong_types_falls_back(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"transfer_count_lifetime": "not-an-int"}))
    sf = StateFile(p)
    s = sf.load()
    assert s.transfer_count_lifetime == 0


def test_save_creates_parent_directory(tmp_path):
    sf = StateFile(tmp_path / "nested" / "dir" / "state.json")
    sf.save(PersistedState(transfer_count_lifetime=1))
    assert sf.load().transfer_count_lifetime == 1


def test_save_does_not_leave_tmp_files_around(tmp_path):
    sf = StateFile(tmp_path / "state.json")
    sf.save(PersistedState(transfer_count_lifetime=7))
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "state.json"]
    assert leftovers == [], f"unexpected files left behind: {leftovers}"
