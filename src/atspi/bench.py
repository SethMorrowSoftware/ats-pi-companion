"""Interactive bench-verification CLI for the ADAM-6060 wiring.

Run on the bench (Pi connected to the ADAM, ADAM wired to the ATS) before
production install. Walks an operator through every input and output:

  - DI verification: prompt the operator to actuate each ATS contact in
    turn, read the DI bits, confirm exactly the expected channel changed.
  - DO verification: drive each relay output one at a time, prompt the
    operator to confirm the matching ASCO input terminal responded
    (LED toggle, contactor click, whatever the ATS exposes).

Exits non-zero if any step failed or was skipped, so the bench
verification can also be scripted as a precondition for the rest of
commissioning.

Entry point::

    atspi-bench --host 192.168.1.251 --port 502 --unit-id 1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import textwrap
from dataclasses import asdict, dataclass
from typing import TextIO

from . import __version__
from .io_adam import (
    DI_EMERGENCY_AVAIL,
    DI_ENGINE_START,
    DI_LOAD_DISCONNECT,
    DI_NORMAL_AVAIL,
    DI_ON_EMERGENCY,
    DI_ON_NORMAL,
    DO_BYPASS_DELAY,
    DO_FORCE_TRANSFER,
    DO_INHIBIT,
    DO_TEST,
    IOAdamDriver,
)

DI_DESCRIPTIONS = [
    (DI_LOAD_DISCONNECT,
     "DI 0 — Load Disconnect contact (ATS terminals 1↔2). "
     "Actuate by pressing the Test button briefly or transferring; this is "
     "a momentary pulse, so you may need to hold."),
    (DI_ON_NORMAL,
     "DI 1 — On-Normal aux 14AA. Closes when the switch is on the utility "
     "(Normal) source."),
    (DI_ON_EMERGENCY,
     "DI 2 — On-Emergency aux 14BA. Closes when the switch is on the "
     "generator (Emergency) source."),
    (DI_NORMAL_AVAIL,
     "DI 3 — 18RX RL6 (Normal source available). Open the utility breaker "
     "upstream briefly to drop this signal."),
    (DI_EMERGENCY_AVAIL,
     "DI 4 — 18RX RL5 (Emergency source available). Disable the generator "
     "to drop this signal."),
    (DI_ENGINE_START,
     "DI 5 — Engine-start sense. Should assert when the ATS calls the "
     "generator (e.g. utility lost)."),
]

DO_DESCRIPTIONS = [
    (DO_TEST,
     "DO 0 — Test pulse (ATS terminals 6-7). Should cause the ATS to "
     "initiate a test transfer cycle."),
    (DO_FORCE_TRANSFER,
     "DO 1 — Force Transfer (terminals 8-9, maintained). Should cause the "
     "ATS to transfer to emergency while asserted. High-consequence; "
     "ensure load is permitted to flip."),
    (DO_INHIBIT,
     "DO 2 — Inhibit Transfer (terminals 10-11, maintained). Should block "
     "any pending transfer."),
    (DO_BYPASS_DELAY,
     "DO 3 — Bypass Transfer Time Delay (terminals 12-13, pulse). Should "
     "cause the ATS to skip any pending time delay."),
]

# Length of the maintained-DO assert during verification. Long enough for an
# operator to confirm the response without leaving the relay closed forever.
DO_ASSERT_HOLD_S = 2.0


@dataclass
class CheckResult:
    """One DI or DO verification step result."""
    channel: str
    description: str
    outcome: str  # "pass" | "fail" | "skip"
    detail: str = ""


def _prompt(text: str, *, stream_in: TextIO, stream_out: TextIO) -> str:
    """Display a prompt and read one line from stream_in. Trimmed and
    lowercased.
    """
    print(text, end=" ", file=stream_out, flush=True)
    line = stream_in.readline()
    if not line:  # EOF — treat as skip
        return ""
    return line.strip().lower()


def _ask_yes_no_skip(
    text: str, *, stream_in: TextIO, stream_out: TextIO,
) -> str:
    """Returns 'yes', 'no', or 'skip'."""
    while True:
        ans = _prompt(text + " [y/N/s(kip)]:", stream_in=stream_in, stream_out=stream_out)
        if ans in ("y", "yes"):
            return "yes"
        if ans in ("n", "no", ""):
            return "no"
        if ans in ("s", "skip"):
            return "skip"
        print("  (please answer y, n, or s)", file=stream_out)


async def _read_dis(driver: IOAdamDriver) -> list[bool]:
    """Read all six DIs as a list of bools, using the driver's configured
    DI function code (io.adam.di_read / --di-read).
    """
    return await driver._read_di_bits(6)  # noqa: SLF001


async def _read_dos(driver: IOAdamDriver) -> list[bool]:
    """Read all six DOs as a list of bools."""
    bits = await driver._read_coils(0x0010, 6)  # noqa: SLF001
    return list(bits)


async def _verify_di(
    driver: IOAdamDriver, ch: int, description: str,
    stream_in: TextIO, stream_out: TextIO,
) -> CheckResult:
    print(file=stream_out)
    print(f"  ── DI {ch} ──", file=stream_out)
    print(textwrap.fill(description, width=78, initial_indent="  ",
                        subsequent_indent="  "), file=stream_out)

    baseline = await _read_dis(driver)
    print(f"  baseline state: {baseline}", file=stream_out)

    ans = _ask_yes_no_skip(
        "  Actuate the signal now and press Enter ('s' to skip):",
        stream_in=stream_in, stream_out=stream_out,
    )
    if ans == "skip":
        return CheckResult(f"DI{ch}", description, "skip", "operator skipped")

    new_state = await _read_dis(driver)
    print(f"  new state:      {new_state}", file=stream_out)

    changed_channels = [i for i in range(6) if baseline[i] != new_state[i]]
    if not changed_channels:
        # Maybe a load-disconnect-style pulse that already returned to rest.
        # Ask the operator if they saw it.
        confirm = _ask_yes_no_skip(
            "  No bit change detected — did the actuation occur (transient OK)?",
            stream_in=stream_in, stream_out=stream_out,
        )
        if confirm == "yes":
            return CheckResult(
                f"DI{ch}", description, "pass",
                "transient pulse not captured by single read — operator confirmed",
            )
        if confirm == "skip":
            return CheckResult(f"DI{ch}", description, "skip", "operator skipped")
        return CheckResult(
            f"DI{ch}", description, "fail",
            "no bit change detected and operator did not confirm",
        )

    if changed_channels == [ch]:
        return CheckResult(f"DI{ch}", description, "pass",
                           f"channel {ch} changed as expected")

    return CheckResult(
        f"DI{ch}", description, "fail",
        f"expected channel {ch} to change, but channels {changed_channels} changed — "
        "wiring or channel-mapping mismatch",
    )


async def _verify_do(
    driver: IOAdamDriver, ch: int, description: str,
    stream_in: TextIO, stream_out: TextIO,
) -> CheckResult:
    print(file=stream_out)
    print(f"  ── DO {ch} ──", file=stream_out)
    print(textwrap.fill(description, width=78, initial_indent="  ",
                        subsequent_indent="  "), file=stream_out)

    ans = _ask_yes_no_skip(
        "  Ready to drive this output? (must be safe to actuate the ATS)",
        stream_in=stream_in, stream_out=stream_out,
    )
    if ans == "skip" or ans == "no":
        return CheckResult(f"DO{ch}", description, "skip",
                           "operator declined to drive")

    # Drive the output. For pulsed channels (test, bypass_delay) we use
    # the driver's normal pulse logic; for maintained channels we
    # assert, hold for the operator to observe, then release.
    if ch == DO_TEST:
        await driver.drive_outputs(test_pulse_ms=1500)
    elif ch == DO_BYPASS_DELAY:
        await driver.drive_outputs(bypass_delay_pulse_ms=1500)
    elif ch == DO_FORCE_TRANSFER:
        # try/finally so an interrupt (Ctrl-C, dropped SSH) during the hold can
        # never strand the ATS in forced-transfer — the release always runs.
        try:
            await driver.drive_outputs(force_transfer=True)
            await asyncio.sleep(DO_ASSERT_HOLD_S)
        finally:
            await driver.drive_outputs(force_transfer=False)
    elif ch == DO_INHIBIT:
        try:
            await driver.drive_outputs(inhibit=True)
            await asyncio.sleep(DO_ASSERT_HOLD_S)
        finally:
            await driver.drive_outputs(inhibit=False)

    # Wait a moment for any pulse to complete + release latency.
    await asyncio.sleep(0.3)

    confirm = _ask_yes_no_skip(
        "  Did the matching ATS terminal respond (LED, contactor, etc.)?",
        stream_in=stream_in, stream_out=stream_out,
    )
    if confirm == "yes":
        return CheckResult(f"DO{ch}", description, "pass",
                           "operator confirmed ATS response")
    if confirm == "skip":
        return CheckResult(f"DO{ch}", description, "skip",
                           "operator skipped confirmation")
    return CheckResult(f"DO{ch}", description, "fail",
                       "operator did not observe ATS response — check wiring")


async def _run(
    host: str, port: int, unit_id: int,
    *,
    di_read: str = "coils",
    skip_dis: bool = False,
    skip_dos: bool = False,
    output_json: bool = False,
    stream_in: TextIO,
    stream_out: TextIO,
) -> int:
    print(f"atspi-bench v{__version__}", file=stream_out)
    print(f"target ADAM-6060: {host}:{port} unit_id={unit_id} di_read={di_read}",
          file=stream_out)

    # Bench verification drives outputs directly with the ATS under LOTO, so it
    # is the one place the F1 hardware-fail-safe gate is explicitly waived (the
    # auditable bench waiver). Production goes through __main__/_build_io_driver,
    # which defaults require_hw_watchdog=True.
    driver = IOAdamDriver(
        host=host, port=port, unit_id=unit_id, di_read=di_read,
        require_hw_watchdog=False,
    )
    connected = await driver.connect()
    if not connected:
        print(f"FAIL: cannot reach ADAM at {host}:{port}", file=stream_out)
        return 2

    results: list[CheckResult] = []
    try:
        if not skip_dis:
            print(file=stream_out)
            print("=== Digital inputs (DI 0-5) ===", file=stream_out)
            for ch, desc in DI_DESCRIPTIONS:
                r = await _verify_di(driver, ch, desc, stream_in, stream_out)
                results.append(r)
                print(f"  result: {r.outcome.upper()}: {r.detail}", file=stream_out)

        if not skip_dos:
            print(file=stream_out)
            print("=== Digital outputs (DO 0-3) ===", file=stream_out)
            print("WARNING: each DO drives an ATS input. Confirm LOTO state",
                  file=stream_out)
            print("and load-flip permissions BEFORE proceeding.", file=stream_out)
            for ch, desc in DO_DESCRIPTIONS:
                r = await _verify_do(driver, ch, desc, stream_in, stream_out)
                results.append(r)
                print(f"  result: {r.outcome.upper()}: {r.detail}", file=stream_out)
    finally:
        # Safety net: never leave ANY command relay asserted on the way out —
        # including on Ctrl-C or an exception mid-test. That covers both the
        # maintained pair (Force Transfer / Inhibit) and a pulsed Test/Bypass
        # interrupted mid-pulse: the pulse-release timer dies with this
        # process, and a bench module typically has no FSV configured yet, so
        # nothing else would drop the relay. _verify_do also releases
        # per-channel; this is the belt-and-suspenders backstop (and the ADAM
        # host watchdog is the hardware layer below it). Only meaningful when
        # we actually drove outputs this run.
        if not skip_dos:
            try:
                await driver.release_all_outputs()
            except Exception as e:  # noqa: BLE001
                print(f"WARNING: failed to release command outputs on exit: {e}",
                      file=stream_out)
        await driver.close()

    return _report(results, output_json=output_json, stream_out=stream_out)


def _report(results: list[CheckResult], *, output_json: bool, stream_out: TextIO) -> int:
    """Print a summary table; return non-zero exit code if any fail or skip."""
    print(file=stream_out)
    print("=== Summary ===", file=stream_out)
    passes = sum(1 for r in results if r.outcome == "pass")
    fails = sum(1 for r in results if r.outcome == "fail")
    skips = sum(1 for r in results if r.outcome == "skip")
    for r in results:
        marker = {"pass": "[OK]", "fail": "[FAIL]", "skip": "[SKIP]"}[r.outcome]
        print(f"  {marker:7} {r.channel}: {r.detail}", file=stream_out)
    print(file=stream_out)
    print(f"  {passes} passed, {fails} failed, {skips} skipped",
          file=stream_out)

    if output_json:
        json.dump([asdict(r) for r in results], stream_out, indent=2)
        print(file=stream_out)

    # Exit 0 only if every executed check passed and at least one ran.
    if not results:
        return 2
    if fails > 0:
        return 1
    if skips > 0:
        # Skips are not failures, but the verification is incomplete —
        # signal that with exit 3 so a commissioning script can distinguish.
        return 3
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="atspi-bench",
        description="Interactive ADAM-6060 wiring verification for the ATS-Pi",
    )
    ap.add_argument("--host", required=True, help="ADAM-6060 IP address")
    ap.add_argument("--port", type=int, default=502)
    ap.add_argument("--unit-id", type=int, default=1, dest="unit_id")
    ap.add_argument("--di-read", default="coils", dest="di_read",
                    choices=["coils", "discrete_inputs"],
                    help="Modbus function code for reading DIs: 'coils' (FC01, "
                         "default) or 'discrete_inputs' (FC02). If the DIs read "
                         "all-0 with the default, re-run with discrete_inputs.")
    ap.add_argument("--skip-dis", action="store_true",
                    help="Skip the digital-input verification block")
    ap.add_argument("--skip-dos", action="store_true",
                    help="Skip the digital-output verification block "
                         "(use when the ATS is energised and load flip is unsafe)")
    ap.add_argument("--json", action="store_true", dest="output_json",
                    help="Also emit results as JSON to stdout after the summary")
    ap.add_argument("--version", action="version", version=f"atspi-bench {__version__}")
    args = ap.parse_args()

    code = asyncio.run(_run(
        host=args.host,
        port=args.port,
        unit_id=args.unit_id,
        di_read=args.di_read,
        skip_dis=args.skip_dis,
        skip_dos=args.skip_dos,
        output_json=args.output_json,
        stream_in=sys.stdin,
        stream_out=sys.stdout,
    ))
    sys.exit(code)


if __name__ == "__main__":
    main()
