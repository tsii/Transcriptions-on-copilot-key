#!/usr/bin/env bash
# install_wayb_0.2.1.sh — venv-free switch for release 0.2.1 (Way B).
# Dry-run by default; root steps and the unit switch run only with --apply.
# The operator runs this (sudo needs a password); production never dies.
# Usage:
#   bash .pi/tools/install_wayb_0.2.1.sh [--dry-run|--apply] [--only=STEP] [--switch-unit] [--rollback]
#   STEP is one of R1..R6. --rollback restores the latest unit backup only.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"

PY=/usr/bin/python3
SRC="$REPO/src"
REQ="$REPO/packaging/runtime-requirements.txt"
DEB="$REPO/dist/stt-button_0.2.1_amd64.deb"
GATE_TOOL="$REPO/.venv/bin/python"
GATE_SCRIPT="$REPO/tools/deps_gate.py"
GATE_REPORT="$REPO/.pi/reports/deps_gate_wayb.md"
UNIT_SRC="/usr/lib/systemd/user/stt-button.service"
UNIT_DST="$HOME/.config/systemd/user/stt-button.service"
LOGDIR="$REPO/.pi/logs"
TS="$(date +%Y%m%d-%H%M%S)"
LOG="$LOGDIR/install_wayb_$TS.log"

mkdir -p "$LOGDIR" "$REPO/.pi/reports"

MODE="dry"
DO_SWITCH=0
DO_ROLLBACK=0
ONLY=""
LAST_BAK=""

for arg in "$@"; do
  case "$arg" in
    --dry-run) MODE="dry" ;;
    --apply) MODE="apply" ;;
    --switch-unit) DO_SWITCH=1 ;;
    --rollback) DO_ROLLBACK=1 ;;
    --only=*) ONLY="${arg#*=}" ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG"; }

run() {
  log "+ $*"
  if [ "$MODE" = "apply" ]; then "$@"; fi
}

die() { log "ABORT: $*"; exit 1; }
precheck() {
  log "== Pruefen (keine Schreibgriffe) =="
  [ -d "$REPO" ] || die "repo missing: $REPO"
  [ -f "$REQ" ] || die "requirements missing: $REQ"
  [ -f "$DEB" ] || die "deb missing: $DEB"
  [ -x "$PY" ] || die "python missing: $PY"
  if [ -f "/usr/lib/python3.12/EXTERNALLY-MANAGED" ]; then
    log "  PEP-668 marker: present"
  else
    log "  WARN: PEP-668 marker missing"
  fi
  log "  unit target: $UNIT_DST"
  log "  unit source: $UNIT_SRC"
  if [ -f "$UNIT_DST" ]; then
    log "  live unit ExecStart: $(grep -m1 '^ExecStart=' "$UNIT_DST" || true)"
  else
    log "  live unit: not present"
  fi
  if [ "$MODE" = "apply" ]; then
    local pyver flag active
    pyver="$("$PY" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))' 2>/dev/null || true)"
    log "  python version: ${pyver:-unknown}"
    flag="$(dpkg -l stt-button 2>/dev/null | awk '/stt-button[[:space:]]/ {print $1; exit}' || true)"
    log "  dpkg state: ${flag:-<none>}"
    active="$(systemctl --user is-active stt-button 2>/dev/null || true)"
    log "  service is-active: ${active:-unknown}"
  else
    log "  (dry-run: python/dpkg/systemctl reads are --apply only)"
  fi
}

needs_purge() {
  local f
  f="$(dpkg -l stt-button 2>/dev/null | awk '/stt-button[[:space:]]/ {print $1; exit}' || true)"
  case "$f" in
    ii|iU|iF|rc|ic) return 0 ;;
    *) return 1 ;;
  esac
}

step_r1() {
  log "== R1: purge previous stt-button package =="
  if [ "$MODE" = "apply" ]; then
    if needs_purge; then
      run sudo dpkg --purge stt-button
      local after
      after="$(dpkg -l stt-button 2>/dev/null | awk '/stt-button[[:space:]]/ {print $1; exit}' || true)"
      case "$after" in
        ""|un) log "  purge ok (state=${after:-none})" ;;
        *) die "purge did not clear package (state=$after)" ;;
      esac
    else
      log "  package not present, nothing to purge"
    fi
  else
    run sudo dpkg --purge stt-button
    log "  (dry-run: purge runs only when dpkg reports the package)"
  fi
}

step_r2() {
  log "== R2: install python3-pip (root) =="
  run sudo apt-get install -y python3-pip
  if [ "$MODE" = "apply" ]; then
    run /usr/bin/python3 -m pip --version
  fi
}

step_r3() {
  log "== R3: pip install --user (torch first, then requirements) =="
  run /usr/bin/python3 -m pip install --user --break-system-packages \
    --index-url https://download.pytorch.org/whl/cpu \
    torch==2.14.1+cpu torchaudio==2.11.0+cpu
  run /usr/bin/python3 -m pip install --user --break-system-packages \
    -r "$REQ"
}

run_gate() {
  local rc=0
  if [ "$MODE" = "apply" ]; then
    set +e
    "$GATE_TOOL" "$GATE_SCRIPT" \
      --python "$PY" --src "$SRC" --requirements "$REQ" \
      --report "$GATE_REPORT"
    rc=$?
    set -e
  fi
  GATE_LEVEL=$rc
}

step_r4() {
  log "== R4: dependency gate (must be Level 0) =="
  run_gate
  if [ "$MODE" = "apply" ]; then
    log "  gate exit level: $GATE_LEVEL"
    if [ "$GATE_LEVEL" -ne 0 ]; then
      if [ -f "$GATE_REPORT" ]; then log "$(cat "$GATE_REPORT")"; fi
      if [ "$GATE_LEVEL" -eq 3 ]; then
        log "  shadow_risk hint: unit needs Environment=PYTHONUSERBASE=%h/.local/stt"
        log "  (and use the same PYTHONUSERBASE when installing)"
      fi
      die "gate not Level 0 (exit $GATE_LEVEL) — abort before install"
    fi
    log "  gate OK (Level 0)"
  else
    log "  (dry-run: gate probe runs only on --apply)"
  fi
}

step_r5() {
  log "== R5: install the 0.2.1 deb (root) =="
  log "  gate Level re-check before install:"
  run_gate
  if [ "$MODE" = "apply" ]; then
    if [ "$GATE_LEVEL" -ne 0 ]; then
      die "gate not Level 0 (exit $GATE_LEVEL) — abort before install"
    fi
  fi
  run sudo apt-get install -y "$DEB"
  if [ "$MODE" = "apply" ]; then
    dpkg -s stt-button 2>/dev/null | grep -q "Status: install ok installed" \
      || die "dpkg -s status is not 'install ok installed'"
    dpkg -L stt-button 2>/dev/null | grep -qE 'bin/stt-button$|systemd/user' \
      || die "expected files missing from dpkg -L"
    log "  install verified"
  fi
}

step_r6() {
  log "== R6: switch the live unit (requires --switch-unit) =="
  if [ "$DO_SWITCH" -ne 1 ]; then
    log "  skipped: pass --switch-unit to switch the live unit"
    return 0
  fi
  [ -f "$UNIT_SRC" ] || die "unit source missing: $UNIT_SRC"
  if [ "$MODE" = "apply" ]; then
    mkdir -p "$(dirname "$UNIT_DST")"
  fi

  if [ -f "$UNIT_DST" ] && cmp -s "$UNIT_SRC" "$UNIT_DST"; then
    log "  unit already matches source; no write, no restart"
    LAST_BAK=""
  else
    local have=0
    local b
    for b in "$UNIT_DST".*.bak; do
      [ -e "$b" ] || continue
      if cmp -s "$UNIT_DST" "$b"; then have=1; break; fi
    done
    if [ -f "$UNIT_DST" ] && [ "$have" -eq 0 ]; then
      LAST_BAK="$UNIT_DST.$TS.bak"
      run cp -a "$UNIT_DST" "$LAST_BAK"
      log "  backup -> $LAST_BAK"
    else
      log "  identical .bak already present, skip backup"
    fi
    run cp -a "$UNIT_SRC" "$UNIT_DST"
    run systemctl --user daemon-reload
    run systemctl --user restart stt-button   # exactly one restart
  fi
  health_check
}

health_check() {
  if [ "$MODE" != "apply" ]; then
    log "  (dry-run: health check is --apply only)"
    return 0
  fi
  sleep 2
  local active nrest errs ready
  active="$(systemctl --user is-active stt-button 2>/dev/null || true)"
  nrest="$(systemctl --user show stt-button -p NRestarts --value 2>/dev/null || true)"
  errs="$(journalctl --user -u stt-button -n 80 --no-pager 2>/dev/null \
    | grep -cE 'ERROR|Traceback' || true)"
  ready="$(journalctl --user -u stt-button -n 80 --no-pager 2>/dev/null \
    | grep -c 'Ready\.' || true)"
  log "  is-active=$active NRestarts=$nrest ERROR/Traceback-lines=$errs Ready-lines=$ready"
  if [ "$active" = "active" ] && [ "$errs" = "0" ] && [ "$ready" -ge 1 ]; then
    log "  HEALTH OK"
  else
    log "  HEALTH BAD — auto-restore last backup"
    if [ -n "$LAST_BAK" ] && [ -f "$LAST_BAK" ]; then
      run cp -a "$LAST_BAK" "$UNIT_DST"
      run systemctl --user daemon-reload
      run systemctl --user restart stt-button   # one restore restart
    else
      die "health bad and no backup available to restore"
    fi
  fi
}

do_rollback() {
  log "== rollback: restore the latest unit backup =="
  local bak
  bak="$(ls -1t "$UNIT_DST".*.bak 2>/dev/null | head -n1 || true)"
  if [ -z "$bak" ]; then
    die "no .bak backup found for $UNIT_DST"
  fi
  log "  restoring: $bak"
  run cp -a "$bak" "$UNIT_DST"
  run systemctl --user daemon-reload
  run systemctl --user restart stt-button   # exactly one restart
  if [ "$MODE" = "apply" ]; then
    log "  is-active: $(systemctl --user is-active stt-button 2>/dev/null || true)"
  fi
  log "  rollback restores the unit only — package and user-site stay untouched"
}

main() {
  precheck
  if [ "$DO_ROLLBACK" -eq 1 ]; then
    do_rollback
  elif [ -n "$ONLY" ]; then
    case "$ONLY" in
      R1) step_r1 ;;
      R2) step_r2 ;;
      R3) step_r3 ;;
      R4) step_r4 ;;
      R5) step_r5 ;;
      R6) step_r6 ;;
      *) die "unknown --only step: $ONLY" ;;
    esac
  else
    step_r1
    step_r2
    step_r3
    step_r4
    step_r5
    step_r6
  fi
  log "== done (mode=$MODE) log: $LOG"
}

main
