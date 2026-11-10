#!/bin/sh
# STT-Button launcher (.deb). Installed as /usr/bin/stt-button.
#
# Every invocation (also 'systemctl --user start stt-button') goes
# through here. Two steps:
#   1. Import check: verify that the core modules are importable
#      with the Ubuntu system python3. Third-party dependencies live
#      in the user site (~/.local/lib/python3.12/site-packages);
#      if they are missing, print a clear pip hint and exit.
#   2. Run the command with the system python
#      (/usr/bin/python3 -m stt_button).
set -eu

ROOT="${STT_BUTTON_ROOT:-/opt/stt-button}"

# Only $ROOT/src - it contains exclusively stt_button, no third-party module
export PYTHONPATH="$ROOT/src"

# Import check of the core modules; on failure print a pip hint
if ! /usr/bin/python3 -c 'import sounddevice, numpy, stt_button'; then
    echo "stt-button: core modules are not importable with /usr/bin/python3." >&2
    echo "  Tip: install the dependencies into your user site, e.g.:" >&2
    echo "    /usr/bin/python3 -m pip install --user '/opt/stt-button[stt]'" >&2
    echo "  (see the requirements in $ROOT/requirements-stt.txt)" >&2
    exit 1
fi

exec /usr/bin/python3 -m stt_button "$@"
