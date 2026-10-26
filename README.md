# STT-Button

Push-to-Talk Diktat über die **Copilot-Taste** deines ThinkPad X1: solange du
die Taste hältst, wird aufgenommen und per **Whisper large-v3-turbo** (lokal,
CPU-only) transkribiert. Der Text erscheint **live** an der aktuellen
Cursor-Position im fokussierten Fenster.

Deutsch und Englisch werden erkannt (mehrsprachiges Modell, Sprache per
Config festlegbar oder automatisch).

## So funktioniert's

```
 Copilot-Taste                 ┌──────────────────────────────┐
 (keyd: F24) ──► KeyListener ──► PushToTalkController         │
                    │              │  pw-record (Mikrofon)    │
                    │              ▼                          │
                    │         StreamTranscriber               │
                    │         (Whisper large-v3-turbo, int8)  │
                    │              │  Teiltext-Deltas         │
                    │              ▼                          │
                    └────────► DeltaTyper ──► wtype ──► Cursor │
                              └──────────────────────────────┘
```

* **Push-to-Talk**: Taste halten = aufnehmen, loslassen = finalisieren.
* **Live-Streaming**: Erste Wörter erscheinen nach ~2–3 s, danach Updates;
  das Modell darf während des Streamings das *letzte* Wort korrigieren
  (Backspace), ältere Wörter werden "committed" und nie mehr angetastet.
* **Lokal & offline-fähig**: Das Modell läuft auf der CPU (CTranslate2 int8);
  die Inferenz braucht beim ersten Mal einen Download von ~1,6 GB.

## Voraussetzungen

* Ubuntu 24.04 (GNOME/Wayland), Python 3.12, User in Gruppe `input`.
* `wtype`, `pw-record`, `wl-copy`, `pactl` (sind auf Ubuntu Standard).
* `keyd` (Systemdienst), der die Copilot-Taste auf `F24` mappt — siehe
  `/etc/keyd/default.conf` unten.

> **Warum keyd?** Die Copilot-Taste sendet physisch `Win+Shift+F23`. Der
> Wayland-Compositor (mutter) grabbt die physische Tastatur, deshalb sieht ein
> normaler User-Prozess dort nichts. keyd übersetzt die Kombination auf das
> saubere `F24` und gibt es auf einem **"keyd virtual keyboard"** aus, das ohne
> sudo lesbar ist. Dieses Konzept ist bereits fertig vorhanden — du musst an
> /etc/keyd nichts ändern.

## Installation (als User, kein sudo)

```bash
cd ~/pi-web-ui/STT-Button
./install.sh
```

Das Skript legt ein venv an, installiert die Abhängigkeiten, kopiert die
Config nach `~/.config/stt-button/config.toml` und richtet einen
**systemd-User-Dienst** ein:

```bash
systemctl --user status stt-button
journalctl --user -u stt-button -f     # Logs
```

## Kalibrierung (einmalig)

Falls deine Taste nicht auf `F24` liegt:

```bash
stt-button calibrate
```

Drücke die Taste, wenn das Tool es verlangt. Es ermittelt Device + Keycode
und schreibt sie in die Config.

## Manuell testen

```bash
# Keycodes + Devices ansehen
stt-button list-keys --codes

# Modell vorwärmen / ASR testen (lädt beim ersten Mal ~1,6 GB)
stt-button test-asr --seconds 5

# Textausgabe testen (tippt "test" ins fokussierte Fenster)
stt-button test-typer --text "hallo welt"

# Manuell laufen lassen (statt systemd)
stt-button run
```

## Konfiguration (`~/.config/stt-button/config.toml`)

```toml
input_device_name = "keyd virtual keyboard"
key_codes = ["KEY_F24"]
language = "de"              # "de", "en", "auto"
model = "large-v3-turbo"     # auch "large-v3", "medium", "small"
compute_type = "int8"        # CPU
audio_source = "auto"        # oder Quellenname aus `pactl list short sources`
typer = "wtype"              # "clipboard" als Fallback für Elektron/GTK4
append_space = true
asr_interval_s = 1.5
min_audio_s = 0.8
max_record_s = 60.0
```

### Audioquelle anpassen

```bash
pactl list short sources   # zeigt die verfügbaren Quellen
```

Trage den gewünschten Namen als `audio_source` ein. `auto` nimmt die
Standardquelle (`pactl get-default-source`, z.B. `deepfilter_source` mit
Rauschunterdrückung).

## Troubleshooting

| Problem | Lösung |
|---|---|
| Taste wird nicht erkannt | `stt-button list-keys` — erscheint `keyd virtual keyboard`? Sonst `systemctl status keyd`. |
| Kein Text erscheint | `stt-button test-typer` — wirkt `wtype` im Ziel-Fenster? Falls nicht: `typer = "clipboard"`. |
| Transkription leer | `stt-button test-asr --seconds 5` — kommt Audio an? Quelle prüfen. |
| Zu langsam | `model = "medium"` oder `"small"` (schneller, weniger genau). |
| Modell lädt nicht | Netzwerk/HF-Cache prüfen; `journalctl --user -u stt-button`. |

## Architektur & Test

* `src/stt_button/` — Module: `config`, `keys`, `audio`, `asr`, `typing`,
  `controller`, `app`, `calibrate`, `__main__`.
* `tests/` — TDD-Suite (113 Tests), komplett ohne Mikrofon/Modell/Wayland.

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

## keyd-Referenz (`/etc/keyd/default.conf`)

```ini
[ids]
*

[main]
leftmeta+leftshift+f23 = f24
```

Diese Zeile schluckt die Windows-Copilot-Kombination und sendet ein sauberes
`F24`, das dann als User lesbar ist.