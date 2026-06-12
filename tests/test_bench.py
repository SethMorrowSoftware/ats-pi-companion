"""Tests for the atspi-bench commissioning CLI.

The CLI talks to a real ADAM-6060; here we exercise its decision logic
against the same FakeClient pattern used in test_io_adam.py and feed
scripted operator answers via a StringIO stdin.
"""
from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass, field

import pytest

from atspi import bench
from atspi.io_adam import (
    DI_COIL_BASE,
    DI_ENGINE_START,
    DI_LOAD_DISCONNECT,
    DI_NORMAL_AVAIL,
    DI_ON_NORMAL,
    DO_COIL_BASE,
    DO_INHIBIT,
)


@dataclass
class FakeResult:
    bits: list[bool] = field(default_factory=list)
    is_err: bool = False

    def isError(self) -> bool:  # noqa: N802 (pymodbus interface)
        return self.is_err


class FakeClient:
    """Scriptable fake. Replaces the driver's _client AFTER construction.

    Setting di_sequence makes successive _read_coils(DI_COIL_BASE) return
    different DI snapshots, simulating an operator actuating contacts.
    """

    def __init__(self):
        self.connected = False
        self.di_sequence: list[list[bool]] = []
        self.di_index = 0
        self.do_bits = [False] * 6
        self.writes: list[tuple[int, bool]] = []

    async def connect(self) -> bool:
        self.connected = True
        return True

    def close(self) -> None:
        self.connected = False

    def _next_di(self, count):
        if self.di_sequence and self.di_index < len(self.di_sequence):
            bits = list(self.di_sequence[self.di_index])
        else:
            bits = [False] * 6
        self.di_index += 1
        return bits

    async def read_coils(self, address, count, slave):
        if address == DI_COIL_BASE:
            bits = self._next_di(count)
        elif address == DO_COIL_BASE:
            bits = list(self.do_bits)
        else:
            bits = [False] * count
        while len(bits) % 8 != 0:
            bits.append(False)
        return FakeResult(bits=bits[:max(count, 8)])

    async def read_discrete_inputs(self, address, count, slave):
        bits = self._next_di(count) if address == DI_COIL_BASE else [False] * count
        while len(bits) % 8 != 0:
            bits.append(False)
        return FakeResult(bits=bits[:max(count, 8)])

    async def write_coil(self, address, value, slave):
        self.writes.append((address, value))
        idx = address - DO_COIL_BASE
        if 0 <= idx < len(self.do_bits):
            self.do_bits[idx] = value
        return FakeResult()


@pytest.fixture
def fake_driver(monkeypatch):
    """Patch IOAdamDriver so bench._run() gets a driver with a FakeClient."""
    fake = FakeClient()

    class _PatchedDriver(bench.IOAdamDriver):
        def __init__(self, host, port=502, unit_id=1, di_read="coils",
                     require_hw_watchdog=False, hw_watchdog=None):
            super().__init__(
                host=host, port=port, unit_id=unit_id, di_read=di_read,
                require_hw_watchdog=require_hw_watchdog, hw_watchdog=hw_watchdog,
            )
            self._client = fake  # noqa: SLF001
            self._connected = True  # noqa: SLF001

        async def connect(self):
            self._connected = True
            return True

    monkeypatch.setattr(bench, "IOAdamDriver", _PatchedDriver)
    return fake


def _scripted_stdin(*lines: str) -> io.StringIO:
    """Return a StringIO containing each line followed by '\\n'."""
    return io.StringIO("\n".join(lines) + "\n")


async def test_bench_di_pass_when_only_expected_channel_changes(fake_driver):
    """Operator actuates DI 1 (On-Normal). Only DI 1 changed → pass for
    DI 1, no-change-but-confirmed prompts for the others (we'll mark them
    skip so we can isolate the DI 1 result).
    """
    # Baseline + post-actuation snapshots for each of 6 DIs in order.
    # For DI 1, simulate "On Normal" flipping. For the rest, no change.
    fake_driver.di_sequence = []
    for ch in range(6):
        baseline = [False] * 6
        if ch == DI_ON_NORMAL:
            new = [False] * 6
            new[DI_ON_NORMAL] = True
        else:
            new = list(baseline)  # unchanged
        fake_driver.di_sequence.extend([baseline, new])

    # Script: skip every DI except DI 1, which we mark as actuated (just
    # press Enter to read), and then 'no' to "no-bit-change confirm"
    # prompts for the others.
    script = []
    for ch in range(6):
        if ch == DI_ON_NORMAL:
            script.append("")  # Enter → actuate read
        else:
            script.append("")  # Enter → actuate read (no bit change)
            script.append("n")  # operator did NOT confirm transient

    stream_in = _scripted_stdin(*script)
    stream_out = io.StringIO()
    code = await bench._run(
        "127.0.0.1", 5020, 1,
        skip_dos=True, stream_in=stream_in, stream_out=stream_out,
    )
    out = stream_out.getvalue()
    assert "DI1" in out
    # DI 1 should be PASS; the five others FAIL (no change, no confirm).
    assert out.count("[OK]") == 1
    assert out.count("[FAIL]") == 5
    # Exit non-zero because of the failures.
    assert code == 1


async def test_bench_di_fail_when_wrong_channel_changes(fake_driver):
    """Operator actuates the wrong contact for the prompted channel —
    the bench tool must catch the wiring-vs-channel mismatch.
    """
    # For DI 0 prompt, actuate DI 3 instead (wiring mistake).
    fake_driver.di_sequence = [
        [False] * 6,  # baseline for DI 0 prompt
        [False, False, False, True, False, False],  # DI 3 changed, not DI 0
        # Then skip the rest by sending 's'.
    ]
    script = [""]  # press Enter on DI 0
    # skip the remaining 5 DIs
    script.extend(["s"] * 5)

    stream_in = _scripted_stdin(*script)
    stream_out = io.StringIO()
    code = await bench._run(
        "127.0.0.1", 5020, 1,
        skip_dos=True, stream_in=stream_in, stream_out=stream_out,
    )
    out = stream_out.getvalue()
    assert "DI0" in out
    assert "FAIL" in out
    # The DI 0 step should fail with a channel-mismatch detail mentioning DI 3.
    assert "channels [3]" in out
    assert code == 1  # at least one failure


async def test_bench_do_pass_when_operator_confirms(fake_driver):
    """Skip DIs entirely. For each DO, accept 'y' to drive and 'y' to confirm."""
    # 4 DOs × 2 prompts each (drive? + observed?)
    script = ["y", "y"] * 4
    stream_in = _scripted_stdin(*script)
    stream_out = io.StringIO()
    code = await bench._run(
        "127.0.0.1", 5020, 1,
        skip_dis=True, stream_in=stream_in, stream_out=stream_out,
    )
    out = stream_out.getvalue()
    assert out.count("[OK]") == 4
    assert code == 0
    # Each DO was driven.
    written_dos = {addr - DO_COIL_BASE for addr, _val in fake_driver.writes}
    assert {0, 1, 2, 3}.issubset(written_dos)


async def test_bench_do_fail_when_operator_does_not_observe(fake_driver):
    """For DO 2 (Inhibit), operator says 'n' to observed prompt → fail."""
    # 4 DOs × 2 prompts each. We accept all but report 'no' for DO 2's
    # observation, 'y' for the rest.
    script = []
    for ch in range(4):
        script.append("y")  # ready to drive
        if ch == DO_INHIBIT:
            script.append("n")  # did NOT observe
        else:
            script.append("y")  # observed
    stream_in = _scripted_stdin(*script)
    stream_out = io.StringIO()
    code = await bench._run(
        "127.0.0.1", 5020, 1,
        skip_dis=True, stream_in=stream_in, stream_out=stream_out,
    )
    out = stream_out.getvalue()
    assert "DO2" in out
    assert "FAIL" in out
    assert code == 1


async def test_bench_skip_returns_exit_3(fake_driver):
    """Operator skips every DI and DO → no failures but verification
    incomplete; exit 3 distinguishes from full-pass (0) and fail (1).
    """
    # 6 DIs: skip each. 4 DOs: decline to drive (== skip).
    script = ["s"] * 6 + ["n"] * 4
    stream_in = _scripted_stdin(*script)
    stream_out = io.StringIO()
    code = await bench._run(
        "127.0.0.1", 5020, 1,
        stream_in=stream_in, stream_out=stream_out,
    )
    out = stream_out.getvalue()
    assert "[SKIP]" in out
    assert code == 3


async def test_bench_di_pulse_confirmed_by_operator(fake_driver):
    """A transient pulse (load disconnect) won't be captured by the
    single read between prompts. The bench tool must accept operator
    confirmation as a pass in that case.
    """
    # DI 0 prompt: baseline + no-change post-read. Operator confirms
    # the actuation occurred.
    fake_driver.di_sequence = [
        [False] * 6,  # baseline
        [False] * 6,  # post-read: pulse already returned to rest
    ]
    script = [
        "",         # press Enter for DI 0 actuation
        "y",        # confirm pulse occurred
        "s", "s", "s", "s", "s",  # skip DIs 1-5
    ]
    stream_in = _scripted_stdin(*script)
    stream_out = io.StringIO()
    code = await bench._run(
        "127.0.0.1", 5020, 1,
        skip_dos=True, stream_in=stream_in, stream_out=stream_out,
    )
    out = stream_out.getvalue()
    # DI 0 is OK; the other 5 are SKIP → exit 3.
    assert "DI0" in out
    assert "[OK]" in out
    assert code == 3


async def test_verify_do_releases_force_transfer_on_interrupt(monkeypatch):
    """If the operator interrupts (Ctrl-C) during the Force-Transfer hold, the
    relay MUST still be released by the try/finally — never strand the ATS in
    forced-transfer. Regression for the bench stranded-relay hardening.
    """
    from atspi.io_adam import DO_FORCE_TRANSFER, IOAdamDriver

    # Bench-style direct use → waive the F1 gate (as bench._run does).
    driver = IOAdamDriver(host="127.0.0.1", port=5020, require_hw_watchdog=False)
    fake = FakeClient()
    driver._client = fake  # noqa: SLF001
    driver._connected = True  # noqa: SLF001

    async def interrupt_the_hold(_secs):
        raise KeyboardInterrupt
    monkeypatch.setattr(bench.asyncio, "sleep", interrupt_the_hold)

    stream_in = _scripted_stdin("y")  # 'y' → ready to drive
    stream_out = io.StringIO()
    with pytest.raises(KeyboardInterrupt):
        await bench._verify_do(
            driver, DO_FORCE_TRANSFER, "force transfer", stream_in, stream_out,
        )

    coil = DO_COIL_BASE + DO_FORCE_TRANSFER
    assert (coil, True) in fake.writes, "should have asserted force_transfer"
    assert (coil, False) in fake.writes, "finally must release force_transfer"
    ft_writes = [v for a, v in fake.writes if a == coil]
    assert ft_writes[-1] is False, "last action on the coil must be release"


async def test_bench_releases_all_command_outputs_on_exit(fake_driver):
    """After a full DO run, the bench tool's _run() safety net leaves all
    four command relays (Test, Force Transfer, Inhibit, Bypass) de-energised.
    """
    from atspi.io_adam import DO_BYPASS_DELAY, DO_FORCE_TRANSFER, DO_INHIBIT, DO_TEST

    script = ["y", "y"] * 4  # drive + confirm each DO
    stream_in = _scripted_stdin(*script)
    stream_out = io.StringIO()
    await bench._run(
        "127.0.0.1", 5020, 1,
        skip_dis=True, stream_in=stream_in, stream_out=stream_out,
    )
    for do in (DO_TEST, DO_FORCE_TRANSFER, DO_INHIBIT, DO_BYPASS_DELAY):
        last = [v for a, v in fake_driver.writes if a == DO_COIL_BASE + do][-1]
        assert last is False, f"DO{do} must be released by the time _run exits"


async def test_bench_releases_pulsed_output_stranded_by_interrupt(fake_driver, monkeypatch):
    """Ctrl-C while a Test pulse is mid-flight: the pulse-release timer dies
    with the run, and a bench module typically has no FSV configured to clean
    up after us — so the run-exit net must drive the pulsed coil OFF too.
    """
    from atspi.io_adam import DO_TEST

    real_sleep = asyncio.sleep

    async def interrupting_sleep(secs):
        # The 0.3 s post-drive settle inside _verify_do — interrupt there,
        # while the 1.5 s Test pulse is still asserted.
        if secs == 0.3:
            raise KeyboardInterrupt
        await real_sleep(secs)

    monkeypatch.setattr(bench.asyncio, "sleep", interrupting_sleep)

    stream_in = _scripted_stdin("y")  # ready to drive DO 0 (Test)
    stream_out = io.StringIO()
    with pytest.raises(KeyboardInterrupt):
        await bench._run(
            "127.0.0.1", 5020, 1,
            skip_dis=True, stream_in=stream_in, stream_out=stream_out,
        )

    test_writes = [v for a, v in fake_driver.writes if a == DO_COIL_BASE + DO_TEST]
    assert test_writes and test_writes[-1] is False, (
        "run-exit net must drop the stranded Test relay"
    )


async def test_bench_unreachable_adam_exits_2(monkeypatch):
    """If the ADAM is unreachable, the bench tool exits 2 with a clear msg."""
    class _UnreachableDriver(bench.IOAdamDriver):
        async def connect(self):
            return False  # never connected

    monkeypatch.setattr(bench, "IOAdamDriver", _UnreachableDriver)
    stream_in = io.StringIO("")
    stream_out = io.StringIO()
    code = await bench._run(
        "10.255.255.1", 502, 1,
        stream_in=stream_in, stream_out=stream_out,
    )
    assert code == 2
    assert "cannot reach" in stream_out.getvalue()


def test_bench_main_runs(monkeypatch, capsys):
    """Smoke test: the main() entry point parses args and runs."""
    class _NopDriver(bench.IOAdamDriver):
        async def connect(self):
            return False  # short-circuit out via the "cannot reach" path

    monkeypatch.setattr(bench, "IOAdamDriver", _NopDriver)
    monkeypatch.setattr("sys.argv", ["atspi-bench", "--host", "10.255.255.1"])
    with pytest.raises(SystemExit) as ex:
        bench.main()
    assert ex.value.code == 2
    # Tests use FakeClient/stdin redirection paths separately; this just
    # verifies the argparse wiring.


# Used by some tests; kept here to silence linter.
_ = DI_ENGINE_START
_ = DI_NORMAL_AVAIL
_ = DI_LOAD_DISCONNECT
