#!/usr/bin/env bash
# Install STT-Button (as user, no sudo).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo "==> Creating Python venv (if needed)"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv --without-pip .venv 2>/dev/null || python3 -m venv .venv
fi

if [ ! -x .venv/bin/pip ]; then
    echo "==> Fetching get-pip.py"
    curl -s https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
    .venv/bin/python /tmp/get-pip.py -q
fi

echo "==> Installing dependencies"
.venv/bin/pip install -q -e '.[dev]'

echo "==> Preparing configuration"
mkdir -p "$HOME/.config/stt-button"
if [ ! -f "$HOME/.config/stt-button/config.toml" ]; then
    cp config.example.toml "$HOME/.config/stt-button/config.toml"
    echo "    Default config created (~/.config/stt-button/config.toml)"
fi

echo "==> Installing systemd user unit"
mkdir -p "$HOME/.config/systemd/user"
# Dev mode: venv python directly (install.sh just built it). The .deb path
# uses the self-healing launcher instead (build-deb.sh).
sed "s|@EXEC@|$ROOT/.venv/bin/stt-button run|" systemd/stt-button.service > "$HOME/.config/systemd/user/stt-button.service"
systemctl --user daemon-reload
systemctl --user enable --now stt-button.service 2>/dev/null || true

echo ""
echo "Done!"
echo "  - Warm up the model:  $ROOT/.venv/bin/stt-button test-asr --file test.wav"
echo "  - Test the key:       $ROOT/.venv/bin/stt-button list-keys"
echo "  - Watch the log:      journalctl --user -u stt-button -f"
echo "  - Calibrate:          $ROOT/.venv/bin/stt-button calibrate"
