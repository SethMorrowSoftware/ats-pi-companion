#!/usr/bin/env bash
#
# testadam.sh — bench-verify the ADAM-6060 before enabling the service.
#
# Three steps, fastest-feedback first:
#   1. ping the ADAM (reachability)
#   2. prove Modbus TCP works and print a live snapshot of the 6 DIs + relays
#   3. launch the interactive atspi-bench wiring walkthrough (each DI/DO)
#
# Host/port/unit-id default to /etc/atspi/config.yaml (io.adam.*), then to
# 192.168.1.251:502 unit 1. Flags below pass through to atspi-bench.
#
# Needs the package installed (run ./install.sh first, or a dev venv). No root
# required, though running as root lets it read a 0640 /etc/atspi/config.yaml.
#
set -euo pipefail

CONFIG_FILE="${CONFIG_FILE:-/etc/atspi/config.yaml}"
VENV_DIR="${VENV_DIR:-/opt/atspi/venv}"

HOST="" ; PORT="" ; UNIT_ID="" ; DI_READ="" ; PASSTHRU=()

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
testadam.sh — bench-verify the ADAM-6060 before enabling the service.

Usage: ./testadam.sh [--host IP] [--port N] [--unit-id N] [--di-read coils|discrete_inputs] [--skip-dis] [--skip-dos] [--json]

  --host/--port/--unit-id   override the target (default: read /etc/atspi/config.yaml,
                            then 192.168.1.251:502 unit 1)
  --di-read                 Modbus function code for the DIs: coils (FC01, default) or
                            discrete_inputs (FC02). Default reads io.adam.di_read from
                            config. If the DI snapshot is all-0, try discrete_inputs.
  --skip-dis                skip the digital-input checks (pass-through to atspi-bench)
  --skip-dos                skip driving relays — use when the ATS is energised and a
                            load flip is unsafe (pass-through to atspi-bench)
  --json                    also emit per-step results as JSON (pass-through)
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --host)    HOST="${2:?--host needs a value}"; shift 2 ;;
    --port)    PORT="${2:?--port needs a value}"; shift 2 ;;
    --unit-id) UNIT_ID="${2:?--unit-id needs a value}"; shift 2 ;;
    --di-read) DI_READ="${2:?--di-read needs a value}"; shift 2 ;;
    --skip-dis|--skip-dos|--json) PASSTHRU+=("$1"); shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "unknown argument: $1" ;;
  esac
done

# ── Locate the installed tooling (prefer the venv; fall back to PATH) ─────────
if [ -x "$VENV_DIR/bin/atspi-bench" ]; then
  BENCH="$VENV_DIR/bin/atspi-bench"
elif command -v atspi-bench >/dev/null 2>&1; then
  BENCH="$(command -v atspi-bench)"
else
  die "atspi-bench not found. Run ./install.sh first (or activate your dev venv)."
fi
# Use the Python that lives beside atspi-bench — it has pymodbus + PyYAML.
BENCH_DIR="$(dirname "$BENCH")"
if   [ -x "$BENCH_DIR/python3" ]; then PY="$BENCH_DIR/python3"
elif [ -x "$BENCH_DIR/python"  ]; then PY="$BENCH_DIR/python"
else PY="python3"; fi

# ── Resolve target from config, then defaults ────────────────────────────────
read_cfg() {  # $1 = dotted path under the YAML root; prints value or nothing
  "$PY" - "$CONFIG_FILE" "$1" <<'PY' 2>/dev/null || true
import sys, yaml
try:
    with open(sys.argv[1]) as f:
        d = yaml.safe_load(f) or {}
except Exception:
    sys.exit(0)
for key in sys.argv[2].split("."):
    if not isinstance(d, dict):
        sys.exit(0)
    d = d.get(key)
    if d is None:
        sys.exit(0)
print(d)
PY
}

[ -n "$HOST" ]    || HOST="$(read_cfg io.adam.host)"
[ -n "$HOST" ]    || HOST="192.168.1.251"
[ -n "$PORT" ]    || PORT="$(read_cfg io.adam.port)"
[ -n "$PORT" ]    || PORT="502"
[ -n "$UNIT_ID" ] || UNIT_ID="$(read_cfg io.adam.unit_id)"
[ -n "$UNIT_ID" ] || UNIT_ID="1"
[ -n "$DI_READ" ] || DI_READ="$(read_cfg io.adam.di_read)"
[ -n "$DI_READ" ] || DI_READ="coils"

say "Target ADAM-6060: $HOST:$PORT (unit $UNIT_ID, di_read=$DI_READ)"

# ── 1. Ping ──────────────────────────────────────────────────────────────────
say "1/3  ping ..."
if ping -c 3 -W 2 "$HOST" >/dev/null 2>&1; then
  say "     reachable"
else
  warn "     ping failed — some networks block ICMP, so continuing to the Modbus check."
fi

# ── 2. Modbus reachability + live snapshot ───────────────────────────────────
say "2/3  Modbus read (live DI + relay snapshot) ..."
"$PY" - "$HOST" "$PORT" "$UNIT_ID" "$DI_READ" <<'PY' \
  || die "Modbus read failed — ADAM unreachable or wrong host/port/unit. Fix this before bench-testing."
import asyncio, sys
from pymodbus.client import AsyncModbusTcpClient

host, port, unit = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
di_read = sys.argv[4] if len(sys.argv) > 4 else "coils"
DI = ["DI0  load-disconnect (transfer pulse)",
      "DI1  on-normal aux 14AA",
      "DI2  on-emergency aux 14BA",
      "DI3  normal source available (18RX RL6)",
      "DI4  emergency source available (18RX RL5)",
      "DI5  engine-start sense"]
DO = ["DO0  test", "DO1  force-transfer", "DO2  inhibit",
      "DO3  bypass-delay", "DO4  (spare)", "DO5  (spare)"]

async def main() -> int:
    client = AsyncModbusTcpClient(host=host, port=port, timeout=2.0, retries=1)
    if not await client.connect():
        print(f"     could not open a TCP connection to {host}:{port}")
        return 1
    # DIs via the configured function code (FC02 discrete inputs, or FC01
    # coils); relays are always coils. Mirrors io_adam.IOAdamDriver.
    if di_read == "discrete_inputs":
        di = await client.read_discrete_inputs(address=0x0000, count=6, slave=unit)
    else:
        di = await client.read_coils(address=0x0000, count=6, slave=unit)
    do = await client.read_coils(address=0x0010, count=6, slave=unit)
    client.close()
    if di.isError() or do.isError():
        print(f"     Modbus error (di_read={di_read}): DI={di} DO={do}")
        print("     if DI errored, try the other di_read mode "
              "(--di-read discrete_inputs / coils).")
        return 1
    print("     digital inputs:")
    for label, bit in zip(DI, list(di.bits)[:6]):
        print(f"       [{int(bit)}] {label}")
    print("     relay outputs (read-back):")
    for label, bit in zip(DO, list(do.bits)[:6]):
        print(f"       [{int(bit)}] {label}")
    return 0

sys.exit(asyncio.run(main()))
PY
say "     Modbus OK — the ADAM is reachable and answering."

# ── 3. Interactive wiring verification ───────────────────────────────────────
say "3/3  launching atspi-bench (interactive per-channel verification) ..."
echo "     It walks each DI (actuate the contact) and each DO (confirm the ATS"
echo "     terminal responds). Add --skip-dos if a load flip is unsafe right now."
echo
exec "$BENCH" --host "$HOST" --port "$PORT" --unit-id "$UNIT_ID" --di-read "$DI_READ" ${PASSTHRU[@]+"${PASSTHRU[@]}"}
