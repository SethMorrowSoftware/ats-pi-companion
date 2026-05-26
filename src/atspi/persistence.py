"""Persistent state for counters and timestamps that must survive
process restarts (ICD §9.3).

Writes are atomic via the write-temp + rename pattern: the kernel
guarantees a power loss either keeps the old file intact or shows the
new file in full — never a half-written one. Reads tolerate a missing
or corrupt file by falling back to zeros and logging a warning; that
matches the "MUST reset on reboot" defaults for everything except the
fields tracked here.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("atspi.persistence")


@dataclass
class PersistedState:
    """The subset of register-store state persisted across restarts."""

    transfer_count_lifetime: int = 0
    last_transfer_to_gen_ts: int = 0
    last_retransfer_to_util_ts: int = 0
    # UTC epoch seconds for every transfer-to-generator event still within
    # the 24-hour rolling window. ICD §9.3 says this MAY reset on reboot,
    # but persisting it removes an ops papercut: a service restart for any
    # unrelated reason no longer wipes the 24h count mid-day.
    recent_transfer_wallclocks: list[int] = field(default_factory=list)


class StateFile:
    """JSON-backed persistence with atomic-rename writes."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> PersistedState:
        if not self.path.exists():
            log.info("no persisted state at %s; starting from zeros", self.path)
            return PersistedState()
        try:
            with self.path.open() as f:
                data = json.load(f)
            raw_wallclocks = data.get("recent_transfer_wallclocks") or []
            if not isinstance(raw_wallclocks, list):
                raw_wallclocks = []
            recent_wallclocks = [int(ts) for ts in raw_wallclocks]
            return PersistedState(
                transfer_count_lifetime=int(data.get("transfer_count_lifetime", 0)),
                last_transfer_to_gen_ts=int(data.get("last_transfer_to_gen_ts", 0)),
                last_retransfer_to_util_ts=int(data.get("last_retransfer_to_util_ts", 0)),
                recent_transfer_wallclocks=recent_wallclocks,
            )
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as e:
            log.warning(
                "persisted state at %s is unreadable (%s); falling back to zeros",
                self.path, e,
            )
            return PersistedState()

    def save(self, state: PersistedState) -> None:
        """Write atomically. Creates the parent directory if needed.

        Durability against power loss requires two fsyncs: one for the
        temp file (to flush its contents to disk before rename) and one
        for the parent directory (so the rename entry itself reaches
        disk). Without the directory fsync, a power loss right after
        rename() can leave the old file intact even though we observed
        the rename "succeed".
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Same directory as the target so os.replace is a true rename
        # within one filesystem (atomic).
        fd, tmp_path = tempfile.mkstemp(
            prefix=self.path.name + ".",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(asdict(state), f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
            # Flush the directory entry so the rename survives power loss.
            try:
                dir_fd = os.open(str(self.path.parent), os.O_RDONLY)
            except OSError:
                # Some filesystems (e.g. FAT) don't support directory open
                # for fsync; the file contents are still durable.
                return
            try:
                os.fsync(dir_fd)
            except OSError:
                # Some filesystems don't support directory fsync either;
                # accept the slight durability gap rather than crashing.
                pass
            finally:
                os.close(dir_fd)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
