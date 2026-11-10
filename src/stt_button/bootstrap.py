"""Dev-only bootstrap helper (no runtime path anymore, Weg B).

With the venv-free installation (Weg B) the runtime is installed directly
into the user site; this module is no longer part of any runtime path and
serves development/legacy setups only.
"""
from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path
from typing import Callable, Sequence

log = logging.getLogger(__name__)

#: System Python that creates the venv.
DEFAULT_PY = "python3"

#: PyTorch CPU index: ``torch[cpu]`` is NOT on PyPI (CUDA builds only),
#: hence the explicit index. The build running here is CPU-only, so the
#: slim CPU index is deliberately used (no 2-GB CUDA wheel).
TORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"

#: Tested torch version from the venv (== exactly what is running).
#: Pinned for reproducibility; on upgrade: change here + requirements
#: together.
TORCH_SPECS = ("torch==2.14.1+cpu", "torchaudio==2.11.0+cpu")

#: Name of the ready-stamp file in the venv (cheap, import-free
#: "everything is installed" marker - avoids a slow ``import torch`` on
#: every service start).
READY_STAMP = ".stt-button-ready"

#: Core modules that a Parakeet dictation needs. If all of them are
#: importable, the venv counts as finished (in addition to the stamp).
_CORE_MODULES = (
    "numpy",
    "torch",
    "transformers",
    "onnxruntime",
    "onnx_asr",
    "faster_whisper",
)


class BootstrapError(RuntimeError):
    """Bootstrap precondition not satisfiable (clear message)."""


def _venv_python(venv_dir: Path) -> Path:
    """Path to the venv's Python interpreter."""
    return Path(venv_dir) / "bin" / "python"


def _resolve_default_runtime_req() -> Path:
    """Finds ``packaging/runtime-requirements.txt``.

    Layouts: source tree (``<repo>/src/stt_button``) and installed
    package (``/opt/stt-button/stt_button``). Both sit one level under
    a ``packaging/`` directory (or two in the source layout).
    """
    here = Path(__file__).resolve().parent
    for base in (here.parent, here.parent.parent, Path("/opt/stt-button")):
        cand = base / "packaging" / "runtime-requirements.txt"
        if cand.is_file():
            return cand
    return here.parent / "packaging" / "runtime-requirements.txt"


#: Default requirements path (resolved at import time).
DEFAULT_RUNTIME_REQ = _resolve_default_runtime_req()


def _default_install_root() -> Path:
    """Project root (with ``pyproject.toml``) the code is installed from."""
    here = Path(__file__).resolve().parent
    for base in (here.parent, here.parent.parent, Path("/opt/stt-button")):
        if (base / "pyproject.toml").is_file():
            return base
    return here.parent


def build_plan(
    install_root: Path,
    venv_dir: Path,
    runtime_req: Path,
    py_executable: str = DEFAULT_PY,
) -> list[list[str]]:
    """Returns the (pure) sequence of installation commands.

    The order matters: venv -> torch (CPU index) -> ML stack (req) ->
    code (--no-deps). This function runs NOTHING and performs no network
    I/O - it is the testable specification of what the bootstrap does.
    """
    py = str(py_executable)
    vp = str(_venv_python(venv_dir))
    return [
        # 1) create the venv (with pip, as long as python3-venv is installed)
        [py, "-m", "venv", str(venv_dir)],
        # 2) torch + torchaudio from the CPU index (PyPI has CUDA builds only)
        [vp, "-m", "pip", "install", *TORCH_SPECS, "--index-url", TORCH_CPU_INDEX],
        # 3) ML stack (frozen, PyPI-compatible) incl. build tools (setuptools/wheel)
        [vp, "-m", "pip", "install", "-r", str(runtime_req)],
        # 4) the code itself, WITHOUT its (already present) dependencies
        [vp, "-m", "pip", "install", "--no-deps", str(install_root)],
    ]


def ensure_runtime(
    install_root: Path,
    venv_dir: Path,
    ready: Callable[[], bool],
    runner: Callable[[Sequence[str]], int],
    runtime_req: Path | None = None,
    py_executable: str | None = None,
) -> bool:
    """Ensures the Python runtime is operational.

    Args:
        install_root: Project root (with pyproject.toml) of the installed code.
        venv_dir: Target directory of the per-user venv.
        ready: Callable without arguments, ``True`` = venv operational.
        runner: Executes a command list, returns the return code.
        runtime_req: Path to the requirements file (default: ``DEFAULT_RUNTIME_REQ``).
        py_executable: System Python (default: ``DEFAULT_PY``).

    Returns:
        True = runtime operational (already finished OR just finished).
        False = an installation step failed.

    Raises:
        BootstrapError: the requirements file is missing.
    """
    if ready():
        return True

    req = Path(runtime_req) if runtime_req is not None else DEFAULT_RUNTIME_REQ
    if not req.is_file():
        raise BootstrapError(f"runtime requirements not found: {req}")

    plan = build_plan(
        install_root, venv_dir, req, py_executable or DEFAULT_PY
    )
    log.info("Bootstrap: building Python runtime in %s (%d steps) ...",
             venv_dir, len(plan))
    for cmd in plan:
        log.info("Bootstrap: $ %s", " ".join(map(_shell_escape, cmd)))
        rc = runner(cmd)
        if rc != 0:
            log.error("Bootstrap step failed (rc=%s): %s",
                      rc, " ".join(cmd))
            return False
    log.info("Bootstrap: Python runtime ready.")
    return True


def _shell_escape(arg: str) -> str:
    """Minimal display wrapper (not for execution)."""
    return f"'{arg}'" if (" " in arg or "'" in arg) else arg


def requirements_fingerprint(runtime_req: Path) -> str:
    """Short fingerprint of the requirements (detect version changes)."""
    p = Path(runtime_req)
    try:
        data = p.read_bytes()
    except OSError:
        data = b""
    return hashlib.sha256(
        str(TORCH_CPU_INDEX).encode() + b"\0" + data
    ).hexdigest()[:16]


def runtime_ready(
    install_root: Path,
    venv_dir: Path,
    runtime_req: Path | None = None,
) -> bool:
    """Cheap (import-free) readiness check of the venv.

    True when (a) the venv interpreter exists and (b) the ready stamp
    carries the current requirements fingerprint. A different
    fingerprint (e.g. new packages in the package) makes the bootstrap
    run again.
    """
    vp = _venv_python(venv_dir)
    if not vp.is_file():
        return False
    req = Path(runtime_req) if runtime_req is not None else DEFAULT_RUNTIME_REQ
    stamp = Path(venv_dir) / READY_STAMP
    if not stamp.is_file():
        return False
    try:
        return stamp.read_text().strip() == requirements_fingerprint(req)
    except OSError:
        return False


def probe_venv(venv_dir: Path) -> bool:
    """Asks the venv directly: are all core modules importable?

    A single subprocess query (no stamp needed). Used by ``setup`` to
    save the expensive rebuild when a *working* venv already exists
    (e.g. from install.sh).
    """
    vp = _venv_python(venv_dir)
    if not vp.is_file():
        return False
    code = "import " + ",".join(_CORE_MODULES)
    try:
        proc = subprocess.run(
            [str(vp), "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc.returncode == 0
    except OSError:
        return False


def mark_ready(venv_dir: Path, runtime_req: Path | None = None) -> None:
    """Writes the ready stamp after a successful installation."""
    req = Path(runtime_req) if runtime_req is not None else DEFAULT_RUNTIME_REQ
    stamp = Path(venv_dir) / READY_STAMP
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(requirements_fingerprint(req) + "\n")
    except OSError as exc:
        log.warning("Could not write ready stamp (%s)", exc)


def default_runner(cmd: Sequence[str]) -> int:
    """Production runner: executes a command, returns the return code."""
    try:
        proc = subprocess.run(list(cmd), stdin=subprocess.DEVNULL)
        return proc.returncode
    except OSError as exc:
        log.error("Bootstrap command not executable (%s): %s", exc, cmd)
        return 127


def run_bootstrap(
    install_root: Path | None = None,
    venv_dir: Path | None = None,
) -> bool:
    """Full bootstrap with production defaults.

    The venv lives by default in the per-user environment
    (``~/.local/stt-button/venv``) - clean ownership: every user builds
    their own venv, no root/user conflict. The .deb launcher and
    ``stt-button setup`` use this default. ``install.sh`` (dev) instead
    explicitly creates the venv in the project root (``.venv``).

    The ASR model is NOT fetched here - the engine loads it from the
    Hugging Face cache on the first dictation.
    """
    import os

    root = Path(install_root) if install_root else _default_install_root()
    venv = Path(venv_dir) if venv_dir else Path(
        os.path.expanduser("~/.local/stt-button/venv")
    )
    req = DEFAULT_RUNTIME_REQ

    ok = ensure_runtime(
        install_root=root,
        venv_dir=venv,
        ready=lambda: runtime_ready(root, venv, req),
        runner=default_runner,
        runtime_req=req,
    )
    if ok:
        mark_ready(venv, req)
    return ok
