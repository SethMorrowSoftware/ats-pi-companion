"""Advantech ADAM-6060 driver — production I/O backend.

Implements the abstract :class:`IODriver` against an ADAM-6060 (6 DI +
6 relay DO) over Modbus TCP. Channel-to-signal mapping is the one
documented in ``docs/HARDWARE.md §3``:

  DI 0 → Load Disconnect contact (pulse → position=transferring)
  DI 1 → On Normal aux (14AA)
  DI 2 → On Emergency aux (14BA)
  DI 3 → Normal source available (18RX RL6)
  DI 4 → Emergency source available (18RX RL5)
  DI 5 → Engine-start sense

  DO 0 → Momentary Test pulse
  DO 1 → Maintained Force Transfer
  DO 2 → Maintained Inhibit
  DO 3 → Bypass Transfer Time Delay pulse

ADAM-6060 Modbus map (per Advantech ADAM-6000 User Manual rev A4,
verify against the firmware on the actual unit before commissioning):

  Read coils  (FC01) 00001-00006 → DI 0..5
  Read coils  (FC01) 00017-00022 → DO 0..5 (read-back of relay state)
  Write coil  (FC05) 00017-00022 → set DO 0..5

The implementation uses coil access for both directions because it
maps cleanly to single bits and works on every ADAM-6060 firmware
revision in the field. Holding-register packed reads are an alternative
but their bit layout shifted between firmware revisions.

BENCH-VERIFY before deploying:

  1. Confirm DI coil base (some revisions start at 00001, others 10001
     — pymodbus's ``read_discrete_inputs`` vs ``read_coils``).
  2. Confirm DO coil base for read-back.
  3. Confirm bit ordering matches the labelling on the unit's terminals.
  4. Drive each DO individually and verify the matching ATS terminal
     responds before connecting all six at once.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from .io_driver import FAULT_CALIBRATION, InputSnapshot, OutputState

log = logging.getLogger("atspi.io_adam")


# ADAM-6060 coil addresses (PDU offsets, 0-based).
DI_COIL_BASE = 0x0000  # DI 0..5 (PDU base; function code per VALID_DI_READS)
DO_COIL_BASE = 0x0010  # DO 0..5 (read-back and write)

# How the ADAM exposes its 6 digital inputs over Modbus. On the ADAM-6000
# series the canonical mapping reads the DIs as *discrete inputs* (FC02,
# read_discrete_inputs); FC01 (read_coils) reads the *relay outputs*. Some
# firmware also mirrors the DIs into the coil space at the same PDU base, so
# read_coils may work too — it must be confirmed on the bench (see the
# BENCH-VERIFY block above). This is an operator-settable config value
# (io.adam.di_read) so a wrong guess is a one-line change at commissioning,
# not a code edit + redeploy. If the DIs read all-0 / position stays
# "unknown" with FC01, flip to "discrete_inputs". The DO side is always
# coils (FC01 read-back / FC05 write).
VALID_DI_READS = frozenset(["coils", "discrete_inputs"])
_DI_READ_FN = {"coils": "read_coils", "discrete_inputs": "read_discrete_inputs"}

# DI channel assignments per HARDWARE.md §3.
DI_LOAD_DISCONNECT = 0
DI_ON_NORMAL = 1
DI_ON_EMERGENCY = 2
DI_NORMAL_AVAIL = 3
DI_EMERGENCY_AVAIL = 4
DI_ENGINE_START = 5

# DO channel assignments per HARDWARE.md §3.
DO_TEST = 0
DO_FORCE_TRANSFER = 1
DO_INHIBIT = 2
DO_BYPASS_DELAY = 3

# ICD §6.1 pulse range.
PULSE_MIN_MS = 500
PULSE_MAX_MS = 1500

# The Load Disconnect contact pulses momentarily during a transfer;
# we hold "transferring" position for a short window after we see it
# so a 10 Hz sampling loop reliably catches it.
TRANSFERRING_HOLD_S = 2.0

# After driving a relay, allow up to this long for the actual coil read-back
# to catch up before flagging a mismatch. The ADAM's internal scan cycle is
# nominally ≤100 ms; 500 ms gives generous headroom for one full Modbus
# read/write round-trip plus the relay's own actuation delay.
OUTPUT_SETTLING_S = 0.5

# Per-operation Modbus timeout. The ADAM-6060 typically responds within
# ~50 ms on a healthy LAN; 500 ms gives an order of magnitude of slack.
# pymodbus's default of 3 s × 3 retries = 9 s would stall the 10 Hz
# sampling loop badly enough that GenWatch's 1.5 s prime poll sees
# repeated stale snapshots — and on a flaky drop the operator just sees
# the service appearing wedged. With these values, a hard failure is
# detected within ~1 s and the next sampling cycle retries.
ADAM_TIMEOUT_S = 0.5
ADAM_RETRIES = 1

# A pulsed relay (test, bypass) MUST drop when its window elapses. If the
# release write fails — a network/ADAM blip landing on the exact instant of
# release — retry at this cadence until it lands rather than abandoning the
# relay asserted. Leaving the Test relay stuck on would continuously command
# the ATS to test-transfer to the generator; leaving Bypass stuck on would
# defeat every transfer time delay. Mirrors the safety watchdog's
# "retry until the write lands" posture for maintained commands.
PULSE_RELEASE_RETRY_S = 0.25

# Input debounce: a level (maintained) contact must read the same value for
# this many consecutive 10 Hz samples before the driver publishes the change.
# 3 samples ≈ 300 ms — invisible against ATS position changes (which take
# seconds) but enough to reject single-sample contact bounce / EMI pickup on
# a long control-wire run. The momentary Load Disconnect contact (DI 0) is
# deliberately NOT debounced (see _Debouncer / read_inputs). 1 disables it.
DEFAULT_DEBOUNCE_SAMPLES = 3

# The ADAM-6060 has no spare DI for an Auto/Manual sense contact (all six are
# consumed per HARDWARE.md §3), so the ATS mode can't be read from hardware.
# It is instead an operator-asserted constant (config io.adam.assumed_mode),
# which doubles as a command gate: "manual" lets only cmd_inhibit through,
# "unknown" blocks every command (ICD §6 mode policy).
VALID_ASSUMED_MODES = frozenset(["auto", "manual", "test", "unknown"])


class HwWatchdogNotArmedError(RuntimeError):
    """Raised when a command would *assert* an ATS output but the ADAM's
    hardware host-watchdog / DO safety-value fail-safe has not been verified
    as armed (F1). Releases (de-asserting a relay) are always permitted, so the
    comms-loss safety watchdog and the bench cleanup path can still drop relays.
    """


@dataclass(frozen=True)
class HwWatchdogConfig:
    """Where to read the ADAM-6000 host-watchdog / DO safety-value config and
    what counts as "armed" (F1 self-check).

    The ADAM's hardware host-idle watchdog is the *only* thing that releases a
    latched Force-Transfer / Inhibit relay if the Pi itself dies (the software
    watchdog in ``safety.py`` shares fate with the process). It is configured by
    hand at commissioning (``docs/HARDWARE.md §5.1``); this struct lets the
    driver read it back and refuse to arm outputs if it is not.

    **Every address here is BENCH-VERIFY.** The ADAM-6000 Modbus map for the
    watchdog/safety registers lives in the *ADAM-6000 Series User Manual*
    (Appendix B, "Modbus/TCP addresses of ADAM-6000 modules") and varies by
    model and firmware revision — the same discipline as the DI coil base and
    the FC01/FC02 read toggle (``io.adam.di_read``). They are therefore left
    *unset* (``None``) by default and supplied via ``io.adam.hw_watchdog`` so a
    wrong value is a config edit at commissioning, not a code change + redeploy.

    Addresses are PDU offsets (0-based), as passed to pymodbus
    ``read_holding_registers``. A wrong/unconfigured address fails *closed*
    (the check reports "not armed" → outputs refused) rather than silently
    arming — but only the physical cable-pull test (``HARDWARE.md §5.1``)
    proves the fail-safe actually drops the relay, so that test remains the
    real acceptance gate.
    """

    # Host-idle / communication watchdog enable register, and the value that
    # means "enabled" on this firmware.
    enable_register: int | None = None
    enable_expected: int = 1
    # Watchdog timeout register. Stored raw on the wire; ``timeout_scale_s``
    # converts a raw count to seconds (many ADAM revisions count in 0.1 s, i.e.
    # scale 0.1 — BENCH-VERIFY by reading the register at a known timeout). The
    # armed band matches HARDWARE.md §5.1 (5–10 s: longer than a sampling blip,
    # shorter than the 30 s software watchdog).
    timeout_register: int | None = None
    timeout_scale_s: float = 0.1
    timeout_min_s: float = 5.0
    timeout_max_s: float = 10.0
    # First per-DO safety-value register; ``safety_value_count`` consecutive
    # registers are read (ADAM-6060: DO 0..5). Every one MUST be 0/OFF so the
    # relays de-energise on host loss (HARDWARE.md §5.1).
    safety_value_register_base: int | None = None
    safety_value_count: int = 6

    def is_configured(self) -> bool:
        """All three register addresses supplied → the readback can run."""
        return (
            self.enable_register is not None
            and self.timeout_register is not None
            and self.safety_value_register_base is not None
        )


def _bit_pulse(timestamp_mono: float | None, hold_s: float, now_mono: float) -> bool:
    if timestamp_mono is None:
        return False
    return (now_mono - timestamp_mono) < hold_s


class _Debouncer:
    """Per-channel integrator debounce for level (maintained) contacts.

    A channel's published value only flips after the new level has been read
    on ``samples`` consecutive cycles; a single noisy sample resets that
    channel's counter, so bounce never reaches the output. The first call
    seeds the baseline from the raw read, so there is no startup delay before
    the true state is reported.

    Deliberately NOT applied to the momentary Load Disconnect contact (DI 0):
    that pulse is caught on the raw read and stretched by TRANSFERRING_HOLD_S,
    and debouncing it would swallow the very edge we need to detect a transfer.
    """

    def __init__(self, samples: int):
        self._samples = max(1, int(samples))
        self._stable: list[bool] | None = None
        self._candidate_count: list[int] = []

    def update(self, raw: list[bool]) -> list[bool]:
        if self._stable is None or len(self._stable) != len(raw):
            # First read (or a channel-count change): establish the baseline
            # without waiting out the debounce window.
            self._stable = list(raw)
            self._candidate_count = [0] * len(raw)
            return list(self._stable)
        for i, level in enumerate(raw):
            if level == self._stable[i]:
                self._candidate_count[i] = 0
            else:
                self._candidate_count[i] += 1
                if self._candidate_count[i] >= self._samples:
                    self._stable[i] = level
                    self._candidate_count[i] = 0
        return list(self._stable)


class IOAdamDriver:
    """ADAM-6060 driver. Async, retries lazily, fault bits surface in
    the register store via :meth:`set_output_fault` from the sampling
    loop when a write fails.
    """

    def __init__(
        self,
        host: str,
        port: int = 502,
        unit_id: int = 1,
        debounce_samples: int = DEFAULT_DEBOUNCE_SAMPLES,
        assumed_mode: str = "auto",
        di_read: str = "coils",
        require_hw_watchdog: bool = True,
        hw_watchdog: HwWatchdogConfig | None = None,
    ):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        if assumed_mode not in VALID_ASSUMED_MODES:
            raise ValueError(
                f"assumed_mode={assumed_mode!r} invalid; "
                f"expected one of {sorted(VALID_ASSUMED_MODES)}"
            )
        self._assumed_mode = assumed_mode
        if di_read not in VALID_DI_READS:
            raise ValueError(
                f"di_read={di_read!r} invalid; "
                f"expected one of {sorted(VALID_DI_READS)}"
            )
        self._di_read = di_read
        self._client: AsyncModbusTcpClient | None = None
        self._connected = False

        # F1 — hardware fail-safe self-check. Until connect() reads the ADAM's
        # host-watchdog / DO safety-value config back and confirms it is armed,
        # asserting an output is REFUSED (fail closed). Default-on so the only
        # way to drive outputs without the backstop is an explicit, auditable
        # waiver (require_hw_watchdog=False) for bench work. See _verify_hw_watchdog.
        self._require_hw_watchdog = bool(require_hw_watchdog)
        self._hw_watchdog = hw_watchdog
        self._hw_watchdog_ok = not self._require_hw_watchdog
        self._hw_watchdog_detail = (
            "host-watchdog readback waived (require_hw_watchdog=false)"
            if not self._require_hw_watchdog
            else "host-watchdog not yet verified"
        )
        self._hw_watchdog_checked = False

        # Pulse-release scheduling (the ADAM has no notion of pulse;
        # we drive the relay high then schedule a low write).
        self._test_release_task: asyncio.Task | None = None
        self._bypass_release_task: asyncio.Task | None = None

        # Last time we saw DI 0 (Load Disconnect) asserted. Used to
        # report "transferring" position for a brief hold window since
        # the contact is a momentary pulse, not a maintained state.
        self._load_disconnect_seen_mono: float | None = None

        # Debounce for the five level inputs (DI 1-5). DI 0 is momentary and
        # bypasses this (see read_inputs).
        self._debounce = _Debouncer(debounce_samples)

        # Stuck-relay detection. Tracks the last value commanded to each
        # DO and the monotonic timestamp of that command. Read by
        # check_output_consistency() to compare against actual read-back.
        # Pulse release tasks update this with (False, now) when they fire.
        self._commanded_do: dict[int, tuple[bool, float]] = {}

    async def connect(self) -> bool:
        if self._client is None:
            self._client = AsyncModbusTcpClient(
                host=self.host, port=self.port,
                timeout=ADAM_TIMEOUT_S, retries=ADAM_RETRIES,
            )
        try:
            ok = await self._client.connect()
        except Exception as e:  # noqa: BLE001
            log.warning("ADAM connect to %s:%d failed: %s", self.host, self.port, e)
            ok = False
        self._connected = bool(ok)
        if self._connected:
            log.info("ADAM-6060 connected at %s:%d", self.host, self.port)
            # F1: confirm the hardware fail-safe is armed before we are willing
            # to drive any output. Runs on every (re)connect so an ADAM that is
            # swapped or factory-reset mid-service is re-checked on reconnect.
            await self._verify_hw_watchdog()
        return self._connected

    async def close(self) -> None:
        for t in (self._test_release_task, self._bypass_release_task):
            if t is not None and not t.done():
                t.cancel()
        if self._client is not None:
            self._client.close()
            self._client = None
        self._connected = False

    async def read_inputs(self) -> InputSnapshot:
        raw = await self._read_di_bits(6)
        now_mono = time.monotonic()

        # Load Disconnect (DI 0) is a momentary pulse: latch it from the RAW
        # read (debouncing would swallow the edge) and stretch it via
        # TRANSFERRING_HOLD_S below.
        if raw[DI_LOAD_DISCONNECT]:
            self._load_disconnect_seen_mono = now_mono

        # The remaining inputs are level/maintained signals — debounce them so
        # a single noisy sample on a long control-wire run can't flip published
        # state (and can't spuriously drive the position/transfer-count logic).
        bits = self._debounce.update(raw)

        on_normal = bits[DI_ON_NORMAL]
        on_emerg = bits[DI_ON_EMERGENCY]
        transferring = _bit_pulse(self._load_disconnect_seen_mono, TRANSFERRING_HOLD_S, now_mono)

        if transferring:
            position = "transferring"
        elif on_normal and not on_emerg:
            position = "utility"
        elif on_emerg and not on_normal:
            position = "generator"
        else:
            # Both off (mid-stroke) or both on (impossible / fault)
            position = "unknown"

        # ICD §5.1.1 CALIBRATION: both position-sense auxes (14AA + 14BA)
        # asserted at once is physically impossible for a transfer switch — a
        # welded or miswired aux contact — as distinct from the legitimate
        # both-off mid-stroke. Flag it (off the debounced bits, so a transient
        # can't raise it) so GenWatch can tell a sensor fault from a normal
        # transition rather than seeing only a bare position=unknown.
        fault_bits = FAULT_CALIBRATION if (on_normal and on_emerg) else 0

        return InputSnapshot(
            position=position,
            normal_available=bits[DI_NORMAL_AVAIL],
            emergency_available=bits[DI_EMERGENCY_AVAIL],
            engine_start_calling=bits[DI_ENGINE_START],
            # No Auto/Manual sense contact on the ADAM-6060 — operator-asserted
            # via config (default "auto"). Also gates command writes (ICD §6).
            ats_mode=self._assumed_mode,
            fault_bits=fault_bits,
        )

    async def read_output_state(self) -> OutputState:
        bits = await self._read_coils(DO_COIL_BASE, 6)
        return OutputState(
            test_active=bits[DO_TEST],
            inhibit_active=bits[DO_INHIBIT],
            force_transfer_active=bits[DO_FORCE_TRANSFER],
            bypass_delay_active=bits[DO_BYPASS_DELAY],
        )

    async def drive_outputs(
        self,
        *,
        test_pulse_ms: int | None = None,
        inhibit: bool | None = None,
        force_transfer: bool | None = None,
        bypass_delay_pulse_ms: int | None = None,
    ) -> None:
        # F1: never ASSERT an ATS output while the ADAM's hardware fail-safe is
        # unverified — a Pi crash could otherwise strand the relay latched with
        # nothing left to release it. A de-assert (inhibit/force_transfer=False)
        # is always the safe direction, so it is allowed through even here: this
        # is what lets the comms-loss safety watchdog and the bench cleanup path
        # still drop relays. The orchestrator also publishes the matching
        # persistent OUTPUT_FAULT, which GenWatch surfaces as a fault alarm — but
        # this driver-level refusal is the enforcement that actually blocks the
        # relay (GenWatch's authority gate keys on comms/version/unit_id, not
        # fault bits, so it does not refuse commands on OUTPUT_FAULT alone).
        if not self._hw_watchdog_ok and (
            test_pulse_ms is not None
            or bool(inhibit)
            or bool(force_transfer)
            or bypass_delay_pulse_ms is not None
        ):
            raise HwWatchdogNotArmedError(
                "refusing to assert ATS outputs — ADAM host-watchdog fail-safe "
                f"not verified: {self._hw_watchdog_detail}"
            )
        if test_pulse_ms is not None:
            await self._pulse(DO_TEST, "test", test_pulse_ms)
        if inhibit is not None:
            await self._write_coil(DO_COIL_BASE + DO_INHIBIT, bool(inhibit))
            self._record_commanded(DO_INHIBIT, bool(inhibit))
            log.info("ADAM: inhibit %s", "ASSERT" if inhibit else "RELEASE")
        if force_transfer is not None:
            await self._write_coil(DO_COIL_BASE + DO_FORCE_TRANSFER, bool(force_transfer))
            self._record_commanded(DO_FORCE_TRANSFER, bool(force_transfer))
            log.info("ADAM: force_transfer %s", "ASSERT" if force_transfer else "RELEASE")
        if bypass_delay_pulse_ms is not None:
            await self._pulse(DO_BYPASS_DELAY, "bypass_delay", bypass_delay_pulse_ms)

    async def release_all_outputs(self) -> None:
        """Write OFF to all four ATS command DOs (test, force_transfer,
        inhibit, bypass_delay), cancelling any pending pulse-release timers
        first so a half-finished pulse can't re-assert behind our back.

        Releases are always the safe direction, so this runs even while the
        F1 hardware fail-safe is unverified (no ``HwWatchdogNotArmedError``).
        Raises ``OSError`` on the first failed write; the caller retries.

        The spare DOs (4, 5) are deliberately left untouched — this service
        does not own them (HARDWARE.md §3).
        """
        for t in (self._test_release_task, self._bypass_release_task):
            if t is not None and not t.done():
                t.cancel()
        for do_index in (DO_TEST, DO_FORCE_TRANSFER, DO_INHIBIT, DO_BYPASS_DELAY):
            await self._write_coil(DO_COIL_BASE + do_index, False)
            self._record_commanded(do_index, False)
        log.info("ADAM: all ATS command outputs released (DO 0-3 OFF)")

    def _record_commanded(self, do_index: int, value: bool) -> None:
        """Note what we just drove onto ``do_index`` for stuck-relay detection."""
        self._commanded_do[do_index] = (value, time.monotonic())

    def check_output_consistency(self, actual: OutputState) -> bool:
        """Compare ``actual`` (just read from the ADAM) against the last
        commanded state of each DO. Within OUTPUT_SETTLING_S of a write
        any mismatch is tolerated (relay actuation + ADAM scan latency).
        Past that window, a mismatch indicates a stuck relay or
        miswired DO.
        """
        now = time.monotonic()
        actual_for_do = {
            DO_TEST: actual.test_active,
            DO_FORCE_TRANSFER: actual.force_transfer_active,
            DO_INHIBIT: actual.inhibit_active,
            DO_BYPASS_DELAY: actual.bypass_delay_active,
        }
        for do_index, (cmd_value, cmd_ts) in self._commanded_do.items():
            if now - cmd_ts < OUTPUT_SETTLING_S:
                continue
            actual_value = actual_for_do.get(do_index)
            if actual_value is None:
                continue
            if actual_value != cmd_value:
                log.warning(
                    "ADAM DO%d read-back mismatch: commanded=%s actual=%s "
                    "(%.1fs since command) — possible stuck relay",
                    do_index, cmd_value, actual_value, now - cmd_ts,
                )
                return False
        return True

    # ─── F1: hardware host-watchdog fail-safe self-check ──────────────

    def hw_watchdog_ok(self) -> bool:
        """True when the ADAM hardware host-watchdog / DO safety-value fail-safe
        is verified armed (or the check is waived for bench work). While this is
        False the driver refuses to assert outputs and the sampling loop raises a
        persistent OUTPUT_FAULT, which GenWatch surfaces as a fault alarm (F1).
        """
        return self._hw_watchdog_ok

    def hw_watchdog_status(self) -> tuple[bool, str]:
        """``(ok, human_readable_detail)`` for startup logging / health."""
        return self._hw_watchdog_ok, self._hw_watchdog_detail

    async def _verify_hw_watchdog(self) -> None:
        """Read the ADAM-6000 host-watchdog / DO safety-value config back and
        decide whether the hardware fail-safe is armed (F1).

        Armed requires, per ``HARDWARE.md §5.1``: the host watchdog enabled, its
        timeout inside the 5–10 s band, and every DO safety value 0/OFF. Anything
        else — including registers that aren't configured or can't be read — is
        treated as *not armed* (fail closed). Called from ``connect()`` right
        after the socket comes up, so it reads the client directly rather than
        via ``_ensure_connected`` (which would recurse back into ``connect()``).
        """
        if not self._require_hw_watchdog:
            self._set_hw_watchdog_status(
                True, "host-watchdog readback waived (io.adam.require_hw_watchdog=false)"
            )
            return

        cfg = self._hw_watchdog
        if cfg is None or not cfg.is_configured():
            self._set_hw_watchdog_status(
                False,
                "io.adam.hw_watchdog register addresses are not configured — set "
                "them from the ADAM-6000 User Manual (Appendix B) and bench-verify "
                "against the unit (HARDWARE.md §5.1), or set "
                "io.adam.require_hw_watchdog=false for bench work",
            )
            return

        try:
            enable_raw = await self._read_holding_register(cfg.enable_register)
            timeout_raw = await self._read_holding_register(cfg.timeout_register)
            safety_raw = await self._read_holding_registers(
                cfg.safety_value_register_base, cfg.safety_value_count
            )
        except OSError as e:
            self._set_hw_watchdog_status(
                False, f"could not read ADAM host-watchdog registers: {e}"
            )
            return

        problems: list[str] = []
        if enable_raw != cfg.enable_expected:
            problems.append(
                f"host watchdog not enabled (reg {cfg.enable_register:#06x}="
                f"{enable_raw}, expected {cfg.enable_expected})"
            )
        timeout_s = timeout_raw * cfg.timeout_scale_s
        if not (cfg.timeout_min_s <= timeout_s <= cfg.timeout_max_s):
            problems.append(
                f"watchdog timeout {timeout_s:g}s outside the "
                f"[{cfg.timeout_min_s:g}, {cfg.timeout_max_s:g}]s band "
                f"(reg {cfg.timeout_register:#06x} raw={timeout_raw})"
            )
        nonzero = [
            (cfg.safety_value_register_base + i, v)
            for i, v in enumerate(safety_raw)
            if v != 0
        ]
        if nonzero:
            problems.append(
                "DO safety value(s) not OFF: "
                + ", ".join(f"{addr:#06x}={v}" for addr, v in nonzero)
            )

        if problems:
            self._set_hw_watchdog_status(False, "; ".join(problems))
        else:
            self._set_hw_watchdog_status(
                True,
                f"armed (timeout={timeout_s:g}s, all {cfg.safety_value_count} "
                "DO safety values OFF)",
            )

    def _set_hw_watchdog_status(self, ok: bool, detail: str) -> None:
        first = not self._hw_watchdog_checked
        changed = ok != self._hw_watchdog_ok
        self._hw_watchdog_ok = ok
        self._hw_watchdog_detail = detail
        self._hw_watchdog_checked = True
        if not ok:
            # Safety-critical: a non-armed fail-safe means a Pi crash could
            # strand a latched relay. Log loudly on every check — (re)connects
            # are infrequent, so this does not flood the journal.
            log.error("ADAM host-watchdog fail-safe NOT verified — %s", detail)
        elif first or changed:
            log.info("ADAM host-watchdog fail-safe verified: %s", detail)

    # ─── Internal: pulse handling ─────────────────────────────────────

    async def _pulse(self, do_index: int, name: str, duration_ms: int) -> None:
        # ICD §6: writes during an active pulse are IGNORED — the original
        # pulse runs to its scheduled completion without being re-triggered
        # or extended.
        slot = "_test_release_task" if do_index == DO_TEST else "_bypass_release_task"
        prior = getattr(self, slot)
        if prior is not None and not prior.done():
            log.debug("ADAM: %s already pulsing; ignoring re-trigger", name)
            return

        ms = max(PULSE_MIN_MS, min(PULSE_MAX_MS, int(duration_ms)))
        coil = DO_COIL_BASE + do_index
        await self._write_coil(coil, True)
        self._record_commanded(do_index, True)
        log.info("ADAM: pulsing %s for %d ms", name, ms)

        setattr(self, slot, asyncio.create_task(self._release(coil, do_index, name, ms)))

    async def _release(self, coil: int, do_index: int, name: str, after_ms: int) -> None:
        try:
            await asyncio.sleep(after_ms / 1000.0)
        except asyncio.CancelledError:
            return
        # The pulse window has elapsed: the intended state is now OFF. Record
        # that up front — before the write — so stuck-relay detection flags an
        # overstaying relay (commanded=False vs actual=True past the settling
        # window) even while the release write below is still being retried.
        # Without this, a relay stranded ON by a failed release would read
        # back as commanded==actual==True and never raise OUTPUT_FAULT.
        self._record_commanded(do_index, False)
        attempt = 0
        while True:
            attempt += 1
            try:
                await self._write_coil(coil, False)
            except asyncio.CancelledError:
                return
            except OSError as e:
                # First failure is notable; subsequent retries are expected
                # noise during an ADAM/network outage (the sampling loop is
                # already logging the same outage), so keep them at DEBUG.
                log.log(
                    logging.WARNING if attempt == 1 else logging.DEBUG,
                    "ADAM: release of pulsed %s failed (attempt %d): %s; retrying",
                    name, attempt, e,
                )
                try:
                    await asyncio.sleep(PULSE_RELEASE_RETRY_S)
                except asyncio.CancelledError:
                    return
                continue
            if attempt == 1:
                log.info("ADAM: pulsed %s released", name)
            else:
                log.warning("ADAM: pulsed %s released after %d attempts", name, attempt)
            return

    # ─── Internal: Modbus access with implicit reconnect ─────────────

    async def _ensure_connected(self) -> None:
        if self._connected and self._client is not None and self._client.connected:
            return
        # If a previous read/write set self._connected=False, the pymodbus
        # client may be in a half-open state where .connect() returns True
        # but subsequent operations still time out. Close and recreate so
        # we start from a clean socket. (We don't recreate when _connected
        # is still True; in that case the client just needs reconnect().)
        if not self._connected and self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        await self.connect()
        if not self._connected:
            raise ConnectionError(f"ADAM-6060 unreachable at {self.host}:{self.port}")

    async def _read_di_bits(self, count: int = 6) -> list[bool]:
        """Read the digital inputs using the configured Modbus function code.

        ``io.adam.di_read`` selects ``read_coils`` (FC01) or
        ``read_discrete_inputs`` (FC02) — see VALID_DI_READS for why this is a
        toggle. Both function codes use the same PDU base (``DI_COIL_BASE``);
        Modbus keeps coils and discrete inputs in separate address spaces.
        """
        return await self._read_bits(_DI_READ_FN[self._di_read], DI_COIL_BASE, count)

    async def _read_bits(self, fn_name: str, address: int, count: int) -> list[bool]:
        """Read ``count`` bits via the named pymodbus reader (``read_coils`` or
        ``read_discrete_inputs``), with implicit reconnect and error mapping.
        """
        await self._ensure_connected()
        reader = getattr(self._client, fn_name)
        try:
            rr = await reader(address=address, count=count, slave=self.unit_id)
        except (TimeoutError, ModbusException, ConnectionError) as e:
            self._connected = False
            raise OSError(f"ADAM {fn_name}({address}, {count}) failed: {e}") from e
        if rr.isError():
            raise OSError(f"ADAM {fn_name}({address}, {count}) error: {rr}")
        # pymodbus returns more bits than requested (rounded to byte); trim.
        return list(rr.bits[:count])

    async def _read_coils(self, address: int, count: int) -> list[bool]:
        return await self._read_bits("read_coils", address, count)

    async def _write_coil(self, address: int, value: bool) -> None:
        await self._ensure_connected()
        try:
            wr = await self._client.write_coil(address=address, value=value, slave=self.unit_id)
        except (TimeoutError, ModbusException, ConnectionError) as e:
            self._connected = False
            raise OSError(f"ADAM write_coil({address}, {value}) failed: {e}") from e
        if wr.isError():
            raise OSError(f"ADAM write_coil({address}, {value}) error: {wr}")

    async def _read_holding_registers(self, address: int, count: int) -> list[int]:
        """FC03 read used by the host-watchdog self-check (F1).

        Talks to ``self._client`` directly rather than via ``_ensure_connected``:
        the check runs from inside ``connect()`` right after the socket is up, so
        going through ``_ensure_connected`` would recurse back into ``connect()``.
        """
        if self._client is None:
            raise OSError("ADAM read_holding_registers: client not connected")
        try:
            rr = await self._client.read_holding_registers(
                address=address, count=count, slave=self.unit_id
            )
        except (TimeoutError, ModbusException, ConnectionError) as e:
            raise OSError(
                f"ADAM read_holding_registers({address}, {count}) failed: {e}"
            ) from e
        if rr.isError():
            raise OSError(f"ADAM read_holding_registers({address}, {count}) error: {rr}")
        return list(rr.registers[:count])

    async def _read_holding_register(self, address: int) -> int:
        return (await self._read_holding_registers(address, 1))[0]


__all__ = ["HwWatchdogConfig", "HwWatchdogNotArmedError", "IOAdamDriver"]
