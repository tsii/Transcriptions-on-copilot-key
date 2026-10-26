#!/usr/bin/env bash
# STT-Button installieren (als User, ohne sudo).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo "==> Python-venv anlegen (falls noetig)"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv --without-pip .venv 2>/dev/null || python3 -m venv .venv
fi

if [ ! -x .venv/bin/pip ]; then
    echo "==> get-pip.py holen"
    curl -s https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
    .venv/bin/python /tmp/get-pip.py -q
fi

echo "==> Abhaengigkeiten installieren"
.venv/bin/pip install -q -e '.[dev]'

echo "==> Konfiguration vorbereiten"
mkdir -p "$HOME/.config/stt-button"
if [ ! -f "$HOME/.config/stt-button/config.toml" ]; then
    cp config.example.toml "$HOME/.config/stt-button/config.toml"
    echo "    Default-Config erzeugt (~/.config/stt-button/config.toml)"
fi

echo "==> systemd-User-Unit installieren"
mkdir -p "$HOME/.config/systemd/user"
sed "s|@ROOT@|$ROOT|" systemd/stt-button.service > "$HOME/.config/systemd/user/stt-button.service"
systemctl --user daemon-reload
systemctl --user enable --now stt-button.service 2>/dev/null || true

echo ""
echo "Fertig!"
echo "  - Modell vorwaermen:  $ROOT/.venv/bin/stt-button test-asr --file test.wav"
echo "  - Taste testen:       $ROOT/.venv/bin/stt-button list-keys"
echo "  - Log ansehen:        journalctl --user -u stt-button -f"
echo "  - Kalibrieren:        $ROOT/.venv/bin/stt-button calibrate"