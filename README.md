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

## Modelle umschalten

The ASR engine is selected in `~/.config/stt-button/config.toml`. After a
change, restart the service (`systemctl --user restart stt-button`).
Verify a switch first without touching the service:
`nice -n 19 .venv/bin/python tools/model_switch_check.py --config <file>`.

| Backend | Config | RAM (RSS) | Latency (CPU, 8 threads) | Live text |
|---|---|---|---|---|
| Parakeet TDT 0.6B v3, FP32 (**default, production**) | `backend = "parakeet"`<br>`compute_type = "float32"` | ~2-3 GB | RTF ~0.12 | yes |
| Parakeet TDT 0.6B v3, ONNX int8 | `backend = "parakeet"`<br>`compute_type = "int8"` | ~1 GB | RTF ~0.05 | yes |
| Qwen3-ASR 1.7B (opt-in) | `backend = "qwen"`<br>`compute_type = "bfloat16"` | ~4.7-5.0 GB | RTF ~0.5-0.8 | **no (final-only)** |
| faster-whisper | `backend = "whisper"`<br>`model = "large-v3-turbo"` | ~1.6 GB | RTF ~0.43 | yes |

Qwen example:

```toml
backend = "qwen"
compute_type = "bfloat16"   # "float32" works too but needs ~11.7 GB RAM
# model defaults to Qwen/Qwen3-ASR-1.7B-hf (pinned revision)
streaming_mode = "whole_buffer"
finalize_mode = "full"
```

Qwen is **final-only**: no text appears while speaking; on release the
whole recording is transcribed once (wait ~0.5-0.9x the recording length).
Not available with Qwen (no token timestamps): committed mode, the
`windowed`/`buffered` streaming modes, `cached_mid_tail`, segmenting,
the live pause indicator, vocabulary prompts and cancelling. Invalid
combinations are rejected at startup with a `ConfigError`.

### Qwen on the Intel Arc iGPU (XPU)

`qwen_device = "auto"` (default) | `"xpu"` | `"cpu"` applies only to
`backend = "qwen"`. `auto` uses the Arc iGPU when torch can see it and
otherwise falls back to the CPU, writing one log line. `xpu` also falls
back, but logs a warning; startup never fails over this. The measured
GPU RTF is ~0.16-0.28, against ~0.34-0.81 on the CPU. Loading the model
takes ~7 s.

The GPU stack (torch 2.14.1+xpu, ~3.5 GB) lives in a **separate venv
`.venv-xpu`**. The production `.venv` stays CPU-only:

```bash
tools/setup_qwen_xpu.sh                          # user space, no sudo
.venv-xpu/bin/python tools/model_switch_check.py --config <file> --device auto
# -> "device: requested=auto resolved=xpu ..."
```

The GPU compute runtime is a system package and is installed once by the
operator:
`sudo apt install -y intel-opencl-icd libze-intel-gpu1 libze1`.
Until that is done, `.venv-xpu` also reports `resolved=cpu`.

Back to the default: `backend = "parakeet"` and `compute_type = "float32"`.

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest                  # 167 tests
```
