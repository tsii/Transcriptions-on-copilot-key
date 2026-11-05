#!/usr/bin/env bash
# Build .venv-xpu: a SEPARATE venv with torch 2.14.1+xpu for backend="qwen"
# on the Intel Arc iGPU (plan_qwen_xpu.md D1/D5). User space only; the
# production .venv is only *read* (pip freeze / its pip binary drives the
# new interpreter via --python, because python3.12-venv/ensurepip is absent).
#
# Idempotent. Re-run safe. Rollback: rm -rf .venv-xpu
set -euo pipefail
cd "$(dirname "$0")/.."

XPU_VENV=.venv-xpu
TORCH_PIN="torch==2.14.1+xpu"
XPU_INDEX=https://download.pytorch.org/whl/xpu
PIP=(.venv/bin/pip --python "$XPU_VENV/bin/python")

[ -x "$XPU_VENV/bin/python" ] || python3.12 -m venv --without-pip "$XPU_VENV"

# 1) Same pins as production minus torch/torchaudio and the editable project.
REQ=$(mktemp)
trap 'rm -f "$REQ"' EXIT
.venv/bin/pip freeze --exclude-editable | grep -vE '^(torch|torchaudio)==' > "$REQ"
"${PIP[@]}" install -q -r "$REQ"

# 2) torch-xpu. triton-xpu and the Intel runtime wheels live on the xpu
#    index, so it is the primary index; PyPI is the extra index.
if ! timeout 1200 "${PIP[@]}" install -q "$TORCH_PIN" \
        --index-url https://download.pytorch.org/whl/xpu \
        --extra-index-url https://pypi.org/simple; then
    echo "pip download failed (IPv6?) - retrying torch wheel via curl -4" >&2
    WHL=torch-2.14.1+xpu-cp312-cp312-linux_x86_64.whl
    DL=$(mktemp -d)
    curl -4 -fL -o "$DL/$WHL" "$XPU_INDEX/torch-2.14.1%2Bxpu-cp312-cp312-linux_x86_64.whl"
    "${PIP[@]}" install -q "$DL/$WHL" \
        --index-url "$XPU_INDEX" --extra-index-url https://pypi.org/simple
    rm -rf "$DL"
fi

# 3) The project itself (no deps: already pinned above).
"${PIP[@]}" install -q --no-deps -e .

# 4) Report.
if "$XPU_VENV/bin/python" -c "import sys,torch;from stt_button.device import xpu_available as p;ok,r=p();print(torch.__version__,'xpu usable:',ok,r);sys.exit(0 if ok else 3)"; then
    echo "OK: XPU usable. Run qwen with $XPU_VENV/bin/python -m stt_button ..."
else
    echo "XPU not usable (GPU compute runtime missing). Operator, run once:"
    echo "  sudo apt install -y intel-opencl-icd libze-intel-gpu1 libze1"
    echo "Until then qwen_device=auto falls back to CPU."
fi
