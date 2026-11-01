# STT-Button

Push-to-talk dictation using the **Copilot key** on the ThinkPad X1: hold the key → speak → text appears word by word at the cursor position.

## How it works

- **Transcription:** NVIDIA Parakeet TDT 0.6B (local, CPU-only, automatic DE+EN)
- **Streaming:** window-based with timestamp commits (Whisper-like, without hallucinations)
- **Output:** `dotool` (uinput) with a German keyboard layout
- **Trigger:** `keyd` maps the Copilot key to `F24`

## Installation

```bash
cd ~/pi-web-ui/STT-Button
./install.sh        # set up venv + systemd user service
```

### As a Debian/Ubuntu package (.deb)

Build a lightweight `.deb` (code + launcher + unit only, no venv, no model):

```bash
./tools/build-deb.sh
# -> dist/stt-button_<version>_amd64.deb  (~80 KB)
sudo dpkg -i dist/stt-button_<version>_amd64.deb
```

The package deliberately contains **no** Python venv and **no** ASR model:
- The **venv + ML stack** (`torch[cpu]`, `transformers`, `onnxruntime`, …) are set up
  per user under `~/.local/stt-button/venv` on the **first invocation** of
  `/usr/bin/stt-button` (or `stt-button setup`) (~5–15 min, one time only).
- The engine downloads the **ASR model** from Hugging Face on the first dictation
  (~200 MB, then cached under `~/.cache/huggingface`).

Installation requires (pulled in automatically): `python3`,
`python3-venv`, `python3-pip`, `pipewire-bin`, `pulseaudio-utils`, `wtype`.
Optional: `wl-clipboard`, `dotool` (the default typer is `wtype`), `gnome-shell`.

After installation:

```bash
stt-button setup            # build venv + config.seed + user unit
# adjust ~/.config/stt-button/config.toml (key, audio source, typer)
systemctl --user start stt-button
journalctl --user -u stt-button -f
```

## Usage

```bash
systemctl --user status stt-button          # status
journalctl --user -u stt-button -f          # live log
stt-button run                              # start manually
stt-button calibrate                        # recalibrate key
```

The configuration is located at `~/.config/stt-button/config.toml`.

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest                  # 167 tests
```
