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

### Als Debian/Ubuntu-Paket (.deb)

Ein schlankes `.deb` bauen (nur Code + Launcher + Unit, kein venv, kein Modell):

```bash
./tools/build-deb.sh
# -> dist/stt-button_<version>_amd64.deb  (~80 KB)
sudo dpkg -i dist/stt-button_<version>_amd64.deb
```

Das Paket enthaelt bewusst **kein** Python-venv und **kein** ASR-Modell:
- **venv + ML-Stack** (`torch[cpu]`, `transformers`, `onnxruntime`, …) werden
  beim **ersten Aufruf** von `/usr/bin/stt-button` (oder `stt-button setup`)
  pro User unter `~/.local/stt-button/venv` aufgebaut (~5–15 min, einmalig).
- Das **ASR-Modell** laedt die Engine beim ersten Diktat von Hugging Face
  (~200 MB, danach gecacht unter `~/.cache/huggingface`).

Installation erfordert (werden automatisch mitgezogen): `python3`,
`python3-venv`, `python3-pip`, `pipewire-bin`, `pulseaudio-utils`, `wtype`.
Optional: `wl-clipboard`, `dotool` (Standard-Typer ist `wtype`), `gnome-shell`.

Nach der Installation:

```bash
stt-button setup            # venv bauen + config.seed + User-Unit
# ~/.config/stt-button/config.toml anpassen (Taste, Audioquelle, Typer)
systemctl --user start stt-button
journalctl --user -u stt-button -f
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