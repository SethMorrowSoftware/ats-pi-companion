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
# Dependencies come from the hash-pinned lockfile so the two Pis and every
# re-image get byte-identical, tamper-evident wheels (a supply-chain
# substitution fails the hash check). Regenerate after a dependency change:
#   pip-compile --generate-hashes -o requirements.lock pyproject.toml
LOCK="$REPO_DIR/requirements.lock"
if [ -f "$LOCK" ]; then
  say "Installing pinned dependencies (--require-hashes) from requirements.lock"
  "$VENV_DIR/bin/pip" install --quiet --require-hashes -r "$LOCK" \
    || die "pinned dependency install failed (hash mismatch or offline?)"
  # --no-deps: deps already came from the lock; don't let the package pull
  # unpinned transitive versions.
  say "Installing atspi (no-deps) from $REPO_DIR"
  "$VENV_DIR/bin/pip" install --quiet --no-deps "$REPO_DIR" || die "pip install failed"
elif [ "${ATSPI_ALLOW_UNPINNED:-0}" = "1" ]; then
  warn "requirements.lock not found — installing UNPINNED (ATSPI_ALLOW_UNPINNED=1)."
  "$VENV_DIR/bin/pip" install --quiet "$REPO_DIR" || die "pip install failed"
else
  die "requirements.lock not found — refusing to install without hash-pinned deps. \
Regenerate it (pip-compile --generate-hashes -o requirements.lock pyproject.toml) \
or set ATSPI_ALLOW_UNPINNED=1 to override (not recommended for production)."
fi
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

# ── 6. udev rule: stable /dev/atspi-asco symlink for the USB-RS485 adapter ───
# Raw /dev/ttyUSB<n> is not stable across reboot/re-plug; without this the
# hybrid driver can lose ASCO sensing after a power cut. Harmless for the
# TCP-only 'adam' driver.
UDEV_SRC="$REPO_DIR/udev/99-atspi-serial.rules"
UDEV_DST="/etc/udev/rules.d/99-atspi-serial.rules"
if [ -f "$UDEV_SRC" ]; then
  say "Installing udev rule → $UDEV_DST (stable /dev/atspi-asco symlink)"
  install -m 0644 "$UDEV_SRC" "$UDEV_DST"
  if command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules 2>/dev/null || warn "udevadm reload failed; re-plug or reboot to apply"
    udevadm trigger --subsystem-match=tty 2>/dev/null || true
  fi
  if [ -e /dev/atspi-asco ]; then
    say "Serial adapter present at /dev/atspi-asco → $(readlink -f /dev/atspi-asco 2>/dev/null || echo '?')"
  else
    warn "No /dev/atspi-asco yet — plug in the Waveshare adapter, or run 'lsusb'"
    warn "  to find its VID:PID and add a matching line to $UDEV_DST (driver: hybrid only)."
  fi
fi

# ── 7. Pi-level hardware watchdog (resets the Pi on a kernel/USB hang) ────────
# The software watchdog (atspi.service WatchdogSec) can't restart pid 1. This
# drop-in tells systemd to pet the SoC watchdog so a kernel deadlock hard-resets
# the Pi. The ADAM host-watchdog (F1) releases the relays in the interim.
HWWDT_SRC="$REPO_DIR/systemd/system.conf.d/10-atspi-hwwatchdog.conf"
HWWDT_DST="/etc/systemd/system.conf.d/10-atspi-hwwatchdog.conf"
if [ -f "$HWWDT_SRC" ]; then
  say "Installing hardware-watchdog drop-in → $HWWDT_DST"
  mkdir -p /etc/systemd/system.conf.d
  install -m 0644 "$HWWDT_SRC" "$HWWDT_DST"
  # Apply manager config without rebooting. daemon-reexec re-reads system.conf.d.
  systemctl daemon-reexec || warn "daemon-reexec failed; reboot to arm the watchdog"
  ARMED="$(systemctl show -p RuntimeWatchdogUSec --value 2>/dev/null || echo '')"
  if [ -n "$ARMED" ] && [ "$ARMED" != "0" ] && [ "$ARMED" != "infinity" ]; then
    say "Hardware watchdog armed (RuntimeWatchdogUSec=$ARMED)"
  else
    warn "Hardware watchdog NOT armed yet (RuntimeWatchdogUSec=${ARMED:-unset})."
    warn "  Verify /dev/watchdog exists (bcm2835_wdt) and reboot; check with 'wdctl'."
  fi
else
  warn "hw-watchdog drop-in not found at $HWWDT_SRC — skipping (Pi won't self-reset on a kernel hang)"
fi

# ── 8. Time sync (ICD §9.4/§11: <5 s skew is a hard contract) ────────────────
# Both Pis must agree on wall-clock to <5 s or GenWatch raises TIME_SKEW, and
# the persistent transfer timestamps depend on a correct clock. The Pi has no
# battery RTC, so after a power cut it boots at a wrong time until NTP corrects
# it. Enable a time-sync service and report sync status.
if command -v timedatectl >/dev/null 2>&1; then
  say "Enabling NTP time sync (timedatectl set-ntp true)"
  timedatectl set-ntp true 2>/dev/null || warn "could not enable NTP via timedatectl"
  systemctl enable --now systemd-timesyncd 2>/dev/null || true
  if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -qx yes; then
    say "Clock is NTP-synchronized"
  else
    warn "Clock NOT yet NTP-synchronized. On an air-gapped OT VLAN with no"
    warn "  upstream NTP server, point one Pi (or the site router) at the other"
    warn "  per ICD §11, or GenWatch will raise persistent TIME_SKEW alarms."
  fi
else
  warn "timedatectl not found — ensure NTP/chrony keeps this Pi within 5 s of GenWatch (ICD §11)."
fi

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
