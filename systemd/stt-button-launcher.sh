#!/bin/sh
# STT-Button-Launcher (.deb). Wird als /usr/bin/stt-button installiert.
#
# Jeder Aufruf (auch 'systemctl --user start stt-button') laeuft hier durch.
#   1. Python-Runtime sicherstellen: ``run_bootstrap`` ist idempotent und
#      importfrei — ist der pro-User-venv (~/.local/stt-button/venv) schon
#      fertig (Ready-Stamp + requirements-Fingerabdruck), ist das ein billiges
#      No-Op. NUR beim allerersten Mal (oder nach requirements-Wechsel) wird
#      die venv samt ML-Stack gebaut (~5-15 min, torch[cpu] + ...).
#   2. den Befehl mit dem VENV-Python ausfuehren (braucht torch/
#      transformers/onnxruntime, die nur in der venv sind).
#
# Per-User-venv (nicht /opt) = saubere Ownership: der aufrufende User
# baut/verwendet seine eigene venv, kein Root-/User-Konflikt.
set -eu

ROOT="${STT_BUTTON_ROOT:-/opt/stt-button}"
VENV="${STT_BUTTON_VENV:-$HOME/.local/stt-button/venv}"

# 1) venv sicherstellen (System-Python, importfrei, idempotent)
PYTHONPATH="$ROOT/src" python3 -c "
import pathlib, sys
from stt_button import bootstrap
sys.exit(0 if bootstrap.run_bootstrap(
    install_root=pathlib.Path('$ROOT'),
    venv_dir=pathlib.Path('$VENV'),
) else 1)
"

# 2) venv-Python muss jetzt existieren
VP="$VENV/bin/python"
if [ ! -x "$VP" ]; then
    echo "stt-button: venv fehlt ($VP). Der Build hatte fehlgelaufen." >&2
    echo "  Tipp: pruefe die Internetverbindung, dann erneut ausfuehren." >&2
    exit 1
fi

exec "$VP" -m stt_button "$@"
