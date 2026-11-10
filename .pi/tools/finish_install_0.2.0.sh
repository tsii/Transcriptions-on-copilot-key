#!/usr/bin/env bash
# Completes "Weg 3" for stt-button 0.2.0: package -> per-user venv -> user unit
# -> service restart -> verification. Requires the .deb to be dpkg-installed
# first (that one step needs root and is done by the operator).
#
#   sudo dpkg -i dist/stt-button_0.2.0_amd64.deb      # operator, once
#   bash .pi/tools/finish_install_0.2.0.sh            # user-level, this script
#
# Safe to re-run: `stt-button setup` is idempotent and the config file is only
# seeded when missing (verified in src/stt_button/__main__.py::_cmd_setup).
set -uo pipefail

REPO="/home/salmon/pi-web-ui/STT-Button"
WANT="0.2.0"
LOG_DIR="$REPO/.pi/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/finish_install_$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

say() { printf '\n=== %s ===\n' "$*"; }

say "0) guard: is the package fully configured, and can a venv be built?"
HAVE="$(dpkg-query -W -f='${Version}' stt-button 2>/dev/null || true)"
STATE="$(dpkg-query -W -f='${db:Status-Status}' stt-button 2>/dev/null || true)"
if [ "$HAVE" != "$WANT" ] || [ "$STATE" != "installed" ]; then
  echo "stt-button is NOT ready (version: '${HAVE:-none}', dpkg state: '${STATE:-absent}')."
  echo "'unpacked' means dpkg could not configure it - usually missing dependencies."
  echo "Operator must run (root, P5):"
  echo "  sudo apt install -y python3-venv python3-pip && sudo dpkg --configure -a"
  exit 20
fi
echo "installed version: $HAVE (dpkg state: $STATE)"

# The self-healing launcher builds a per-user venv; Ubuntu ships python3
# WITHOUT venv/ensurepip unless python3-venv is installed. Without it the
# bootstrap can only fail, so refuse early instead of half-building a venv.
if ! python3 -c "import venv, ensurepip" >/dev/null 2>&1; then
  echo "python3 cannot create venvs (venv/ensurepip missing)."
  echo "Operator must run (root, P5):  sudo apt install -y python3-venv python3-pip"
  exit 23
fi
echo "python3 venv support: ok ($(python3 -V))"

say "1) backups (config + current user unit)"
TS="$(date +%Y%m%d-%H%M%S)"
BK="$REPO/.pi/backups/$TS"; mkdir -p "$BK"
cp -a ~/.config/stt-button/config.toml "$BK/" 2>/dev/null && echo "config backed up"
cp -a ~/.config/systemd/user/stt-button.service "$BK/" 2>/dev/null && echo "unit backed up"
echo "backup dir: $BK"

say "2) show the unit that is about to be replaced"
grep -E "^ExecStart" ~/.config/systemd/user/stt-button.service || true

say "3) stt-button setup (venv build in ~/.local/stt-button/venv, 5-15 min)"
/usr/bin/stt-button setup
SETUP=$?
if [ $SETUP -ne 0 ]; then
  echo "setup failed (exit $SETUP) - production unit left as it was."
  exit 21
fi

say "4) reload and restart the user service on the packaged path"
systemctl --user daemon-reload
systemctl --user restart stt-button
sleep 6
systemctl --user status stt-button --no-pager | head -12

say "5) verification: code path, health, no errors"
systemctl --user show stt-button.service -p ExecMainStartTimestamp -p NRestarts -p ActiveState
echo "ExecStart now: $(systemctl --user show stt-button.service -p ExecStart --value)"
ERRS=$(journalctl --user -u stt-button --since "-2 min" --no-pager | grep -cE " ERROR | Traceback")
echo "ERROR/Traceback lines in the last 2 min: $ERRS"
journalctl --user -u stt-button --since "-2 min" --no-pager \
  | grep -E "Watching|model loaded|Ready" | tail -4
[ "$ERRS" = "0" ] || { echo "NOT GREEN - see $LOG"; exit 22; }
echo ""
echo "DONE: production runs from /opt/stt-button via /usr/bin/stt-button."
echo "Log: $LOG"
