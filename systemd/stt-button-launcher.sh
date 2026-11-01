#!/bin/sh
# STT-Button launcher (.deb). Installed as /usr/bin/stt-button.
#
# Every invocation (also 'systemctl --user start stt-button') goes
# through here. Two steps:
#   1. Ensure the Python runtime: run_bootstrap is idempotent and
#      import-free - if the per-user venv
#      (~/.local/stt-button/venv) is already ready (ready stamp +
#      requirements fingerprint), this is a cheap no-op. Only on the
#      very first run (or after a requirements change) is the venv
#      built including the ML stack (~5-15 min, torch[cpu] + ...).
#   2. Run the command with the VENV python (which needs torch/
#      transformers/onnxruntime, which only exist in the venv).
#
# Per-user venv (not /opt) = clean ownership: the calling user
# builds/uses their own venv, no root/user conflict.
set -eu

ROOT="${STT_BUTTON_ROOT:-/opt/stt-button}"
VENV="${STT_BUTTON_VENV:-$HOME/.local/stt-button/venv}"

# 1) Secure the venv (system python, import-free, idempotent)
PYTHONPATH="$ROOT/src" python3 -c "
import pathlib, sys
from stt_button import bootstrap
sys.exit(0 if bootstrap.run_bootstrap(
    install_root=pathlib.Path('$ROOT'),
    venv_dir=pathlib.Path('$VENV'),
) else 1)
"

# 2) The venv python must exist by now
VP="$VENV/bin/python"
if [ ! -x "$VP" ]; then
    echo "stt-button: venv missing ($VP). The build had failed." >&2
    echo "  Tip: check the network connection, then run again." >&2
    exit 1
fi

exec "$VP" -m stt_button "$@"
