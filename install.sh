#!/usr/bin/env bash
#
# install.sh — install the ATS-Pi companion service on a Raspberry Pi.
#
# Automates docs/HARDWARE.md §7: creates the service user, installs the
# package into a dedicated virtualenv, drops in the config and the systemd
# unit (with ExecStart pointed at the venv), and reloads systemd.
#
# Idempotent — safe to re-run (upgrades the venv; never clobbers an existing
# /etc/atspi/config.yaml). It deliberately does NOT start the service: you
# must edit the config and bench-verify the ADAM (./testadam.sh) first, so the
# switch is never driven by an unverified mapping. Re-running after a
# `git pull` is the upgrade path.
#
# Override locations via env, e.g.:  sudo VENV_DIR=/srv/atspi/venv ./install.sh
#
set -euo pipefail

ATSPI_USER="${ATSPI_USER:-atspi}"
VENV_DIR="${VENV_DIR:-/opt/atspi/venv}"
CONFIG_DIR="${CONFIG_DIR:-/etc/atspi}"
CONFIG_FILE="$CONFIG_DIR/config.yaml"
UNIT_DST="/etc/systemd/system/atspi.service"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root:  sudo ./install.sh"
command -v systemctl >/dev/null 2>&1 || die "systemd not found; this installer targets a systemd Raspberry Pi."
[ -f "$REPO_DIR/pyproject.toml" ] || die "can't find pyproject.toml next to install.sh — run it from the repo checkout."

# ── 1. Python 3.11+ ──────────────────────────────────────────────────────────
PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "python3 not found. Install it:  sudo apt install python3 python3-venv"
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)'; then
  die "Python 3.11+ required (found $("$PY" -V 2>&1)). Install a newer python3."
fi
say "Using $("$PY" -V 2>&1) at $PY"

# ── 2. Service user + group (the systemd unit runs as atspi:atspi) ───────────
if ! getent group "$ATSPI_USER" >/dev/null; then
  say "Creating system group '$ATSPI_USER'"
  groupadd --system "$ATSPI_USER"
fi
if id "$ATSPI_USER" >/dev/null 2>&1; then
  say "Service user '$ATSPI_USER' already exists"
else
  say "Creating service user '$ATSPI_USER'"
  useradd --system --no-create-home --shell /usr/sbin/nologin -g "$ATSPI_USER" "$ATSPI_USER"
fi

# Serial-device access for driver: hybrid (USB-RS485 → ASCO Group 5). The
# non-root service can't open /dev/ttyUSB0 without 'dialout'. Harmless for
# driver: adam (TCP-only); the systemd unit also sets SupplementaryGroups=dialout.
if getent group dialout >/dev/null; then
  if id -nG "$ATSPI_USER" | tr ' ' '\n' | grep -qx dialout; then
    say "Service user '$ATSPI_USER' already in 'dialout' (serial access)"
  else
    say "Adding '$ATSPI_USER' to 'dialout' (serial access for driver: hybrid)"
    usermod -aG dialout "$ATSPI_USER"
  fi
else
  warn "group 'dialout' not found — for driver: hybrid, grant the service serial access manually"
fi

# ── 3. Virtualenv + package ──────────────────────────────────────────────────
say "Creating/updating virtualenv at $VENV_DIR"
mkdir -p "$(dirname "$VENV_DIR")"
if [ ! -d "$VENV_DIR" ]; then
  "$PY" -m venv "$VENV_DIR" \
    || die "venv creation failed — install the venv module:  sudo apt install python3-venv"
fi
"$VENV_DIR/bin/pip" install --quiet --upgrade pip setuptools wheel \
  || die "failed to bootstrap pip/setuptools/wheel in the venv"
# Non-editable install: the code is copied into the venv (world-readable),
# so the service works regardless of where this checkout lives or its perms.
say "Installing atspi from $REPO_DIR"
"$VENV_DIR/bin/pip" install --quiet "$REPO_DIR" || die "pip install failed"
ATSPI_BIN="$VENV_DIR/bin/atspi"
[ -x "$ATSPI_BIN" ] || die "atspi binary missing at $ATSPI_BIN after install"
say "Installed $("$ATSPI_BIN" --version)"

# ── 4. Config — never clobber an existing one ────────────────────────────────
mkdir -p "$CONFIG_DIR"
if [ -f "$CONFIG_FILE" ]; then
  say "Config already present at $CONFIG_FILE (left untouched)"
else
  say "Installing default config to $CONFIG_FILE"
  install -m 0640 -o root -g "$ATSPI_USER" "$REPO_DIR/config.example.yaml" "$CONFIG_FILE"
  warn "Edit $CONFIG_FILE before starting: set io.driver (adam|hybrid), io.adam.host, site.unit_id"
  warn "  driver: hybrid also needs io.asco_serial (port, baud, status_register + bits) — see HARDWARE.md §3.1"
fi

# ── 5. systemd unit (ExecStart → the venv binary) ────────────────────────────
say "Installing systemd unit → $UNIT_DST"
sed "s|^ExecStart=.*|ExecStart=$ATSPI_BIN --config $CONFIG_FILE|" \
  "$REPO_DIR/systemd/atspi.service" > "$UNIT_DST"
systemctl daemon-reload

# ── Done ─────────────────────────────────────────────────────────────────────
say "Install complete — the service is installed but NOT started (by design)."
cat <<EOF

Finish commissioning before enabling the service:

  1. Edit the config:         sudo nano $CONFIG_FILE
       - io.driver: adam (contact-only) | hybrid (ADAM control + ASCO Group 5
         over USB-RS485; also set io.asco_serial — HARDWARE.md §3.1)
       - io.adam.host: <ADAM-6060 IP>
       - site.unit_id: <GenWatch expected_unit_id>
  2. Bench-verify the ADAM:   sudo $REPO_DIR/testadam.sh
       hybrid: also bench-verify the serial Group 5 map (HARDWARE.md §3.1)
  3. Enable + start on boot:  sudo systemctl enable --now atspi
  4. Watch it come up:        systemctl status atspi
                              journalctl -u atspi -f

Service binary: $ATSPI_BIN
Config:         $CONFIG_FILE
Upgrade later:  (cd "$REPO_DIR" && git pull) && sudo "$REPO_DIR/install.sh"
Ops / runbook:  docs/RUNBOOK.md
EOF
