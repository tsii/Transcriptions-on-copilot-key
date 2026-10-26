# STT-Button

Push-to-Talk Diktat über die **Copilot-Taste** des ThinkPad X1: Taste halten → sprechen → Text erscheint wortweise an der Cursor-Position.

## So funktioniert's

- **Transkription:** NVIDIA Parakeet TDT 0.6B (lokal, CPU-only, DE+EN automatisch)
- **Streaming:** Fenster-basiert mit Zeitstempel-Commit (Whisper-ähnlich, ohne Halluzinationen)
- **Ausgabe:** `dotool` (uinput) mit deutschem Tastaturlayout
- **Trigger:** `keyd` mappt die Copilot-Taste auf `F24`

## Installation

```bash
cd ~/pi-web-ui/STT-Button
./install.sh        # venv + systemd-User-Dienst einrichten
```

## Nutzung

```bash
systemctl --user status stt-button          # Status
journalctl --user -u stt-button -f          # Live-Log
stt-button run                              # manuell starten
stt-button calibrate                        # Taste neu kalibrieren
```

Config liegt unter `~/.config/stt-button/config.toml`.

## Entwicklung

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest                  # 167 Tests
```