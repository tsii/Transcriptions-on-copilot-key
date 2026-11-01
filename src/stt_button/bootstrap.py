"""Self-Healing-Bootstrap fuer das schlanke .deb (Betreiber 31.10.).

Das Debian-Paket enthaelt NUR den Python-Code, die systemd-User-Unit und
Metadaten — kein venv, kein Modell, keine ML-Abhaengigkeiten. Diese
Module baut die Python-Runtime beim *Erststart* selbst auf:

    1. venv anlegen (``python3 -m venv``; ``python3-venv`` ist ein Depends)
    2. torch + torchaudio vom PyTorch-CPU-Index (nicht PyPI!)
    2. ML-Stack aus ``packaging/runtime-requirements.txt`` (PyPI)
    3. den STT-Button-Code selbst (``--no-deps``)

Das ASR-Modell wird nicht hier geholt — die Engine laedt es vom
Hugging-Face-Cache beim ersten Diktat (float32-Produktionspfad).

Wichtig: Dieses Modul darf NUR die Standardbibliothek importieren. Es
wird von ``app.run`` gerufen, BEVOR die venv existiert und die ML-
Pakete installiert sind — ein ``import numpy`` hier wuerde den Boot
in eine Endlosschleife treiben.
"""
from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path
from typing import Callable, Sequence

log = logging.getLogger(__name__)

#: System-Python, das die venv aufbaut.
DEFAULT_PY = "python3"

#: PyTorch-CPU-Index: ``torch[cpu]`` ist NICHT auf PyPI (nur CUDA-Builds),
#: daher expliziter Index. Das hier laufende Build ist CPU-only, daher
#: wird bewusst der schlanke CPU-Index verwendet (keine 2-GB-CUDA-Wheel).
TORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"

#: Getestete Torch-Version aus dem venv (== exakt das, was laeuft). Pinnen
#: fuer Reproduzierbarkeit; bei Upgrade: hier + requirements zusammen aendern.
TORCH_SPECS = ("torch==2.14.1+cpu", "torchaudio==2.11.0+cpu")

#: Name der Ready-Stamp-Datei in der venv (billiges, importfreies
#: "alles ist installiert"-Merkmal — vermeidet ein langsames
#: ``import torch`` bei jedem Dienststart).
READY_STAMP = ".stt-button-ready"

#: Kern-Module, die ein Parakeet-Diktat braucht. Sind alle importierbar,
#: gilt die venv als fertig (zusatz zum Stamp).
_CORE_MODULES = (
    "numpy",
    "torch",
    "transformers",
    "onnxruntime",
    "onnx_asr",
    "faster_whisper",
)


class BootstrapError(RuntimeError):
    """Bootstrap-Voraussetzung nicht erfuellbar (klare Meldung)."""


def _venv_python(venv_dir: Path) -> Path:
    """Pfad zum Python-Interpreter der venv."""
    return Path(venv_dir) / "bin" / "python"


def _resolve_default_runtime_req() -> Path:
    """Findet ``packaging/runtime-requirements.txt``.

    Layouts: Source-Tree (``<repo>/src/stt_button``) und installiertes
    Paket (``/opt/stt-button/stt_button``). Beide liegen eine Ebene
    unter einem ``packaging/``-Ordner (bzw. zwei im Source-Layout).
    """
    here = Path(__file__).resolve().parent
    for base in (here.parent, here.parent.parent, Path("/opt/stt-button")):
        cand = base / "packaging" / "runtime-requirements.txt"
        if cand.is_file():
            return cand
    return here.parent / "packaging" / "runtime-requirements.txt"


#: Defaults-requirements-Pfad (bei Importzeit resolviert).
DEFAULT_RUNTIME_REQ = _resolve_default_runtime_req()


def _default_install_root() -> Path:
    """Projekt-Root (mit ``pyproject.toml``), aus dem der Code installiert wird."""
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
    """Liefert die (reine) Folge der Installations-Commands.

    Reihenfolge ist bedeutend: venv -> torch(CPU-Index) -> ML-Stack(req)
    -> Code(--no-deps). Diese Funktion fuehrt NICHTS aus und netzwerkt
    nicht — sie ist die testbare Spezifikation, was der Bootstrap macht.
    """
    py = str(py_executable)
    vp = str(_venv_python(venv_dir))
    return [
        # 1) venv anlegen (mit pip, solange python3-venv installiert ist)
        [py, "-m", "venv", str(venv_dir)],
        # 2) torch + torchaudio vom CPU-Index (PyPI hat nur CUDA-Builds)
        [vp, "-m", "pip", "install", *TORCH_SPECS, "--index-url", TORCH_CPU_INDEX],
        # 3) ML-Stack (frozen, PyPI-faehig) inkl. Build-Tools (setuptools/wheel)
        [vp, "-m", "pip", "install", "-r", str(runtime_req)],
        # 4) den Code selbst, OHNE seine (schon da) Dependencies
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
    """Stellt sicher, dass die Python-Runtime lauffaehig ist.

    Args:
        install_root: Projekt-Root (mit pyproject.toml) des installierten Codes.
        venv_dir: Ziel-Ordner der pro-User-venv.
        ready: Callable ohne Argument, ``True`` = venv lauffaehig.
        runner: Fuehrt ein Command-List aus, liefert den Return-Code.
        runtime_req: Pfad zur requirements-Datei (Default: ``DEFAULT_RUNTIME_REQ``).
        py_executable: System-Python (Default: ``DEFAULT_PY``).

    Returns:
        True = Runtime lauffaehig (bereits fertig ODER jetzt fertiggestellt).
        False = ein Installations-Schritt ist fehlgeschlagen.

    Raises:
        BootstrapError: die requirements-Datei fehlt.
    """
    if ready():
        return True

    req = Path(runtime_req) if runtime_req is not None else DEFAULT_RUNTIME_REQ
    if not req.is_file():
        raise BootstrapError(f"runtime-requirements nicht gefunden: {req}")

    plan = build_plan(
        install_root, venv_dir, req, py_executable or DEFAULT_PY
    )
    log.info("Bootstrap: baue Python-Runtime in %s (%d Schritte) ...",
             venv_dir, len(plan))
    for cmd in plan:
        log.info("Bootstrap: $ %s", " ".join(map(_shell_escape, cmd)))
        rc = runner(cmd)
        if rc != 0:
            log.error("Bootstrap-Schritt fehlgeschlagen (rc=%s): %s",
                      rc, " ".join(cmd))
            return False
    log.info("Bootstrap: Python-Runtime bereit.")
    return True


def _shell_escape(arg: str) -> str:
    """Minimaler Anzeige-Wrapper (nicht zum Ausfuehren)."""
    return f"'{arg}'" if (" " in arg or "'" in arg) else arg


def requirements_fingerprint(runtime_req: Path) -> str:
    """Kurzer Fingerabdruck der requirements (Version-Wechsel erkennen)."""
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
    """Billiger (importfreier) Fertigkeits-Check der venv.

    True, wenn (a) der venv-Interpreter existiert, (b) die Ready-Stamp
    den aktuellen requirements-Fingerabdruck tragt. Ein abweichender
    Fingerabdruck (z.B. neue Pakete im Paket) laesst den Bootstrap
    neu laufen.
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


_CORE_MODULES: tuple[str, ...] = (
    "numpy", "torch", "transformers", "onnxruntime", "onnx_asr", "faster_whisper",
)


def probe_venv(venv_dir: Path) -> bool:
    """Fragt die venv direkt ab: sind alle Kern-Module importierbar?

    Eine einzelne Subprozesses-Anfrage (kein Stamp noetig). Wird von
    ``setup`` genutzt, um bei einer *bereits* funktionierenden venv
    (z.B. install.sh) den teuren Rebuild zu sparen.
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
    """Schreibt die Ready-Stamp nach einer erfolgreichen Installation."""
    req = Path(runtime_req) if runtime_req is not None else DEFAULT_RUNTIME_REQ
    stamp = Path(venv_dir) / READY_STAMP
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(requirements_fingerprint(req) + "\n")
    except OSError as exc:
        log.warning("Konnte Ready-Stamp nicht schreiben (%s)", exc)


def default_runner(cmd: Sequence[str]) -> int:
    """Produktions-Runner: fuehrt ein Command aus, gibt Return-Code zurueck."""
    try:
        proc = subprocess.run(list(cmd), stdin=subprocess.DEVNULL)
        return proc.returncode
    except OSError as exc:
        log.error("Bootstrap-Command nicht ausfuehrbar (%s): %s", exc, cmd)
        return 127


def run_bootstrap(
    install_root: Path | None = None,
    venv_dir: Path | None = None,
) -> bool:
    """Komplett-Bootstrap mit Produktion-defaults.

    Die venv liegt standardmaessig in der pro-User-Umgebung
    (``~/.local/stt-button/venv``) — saubere Ownership: jeder User baut
    seine eigene venv, kein Root-/User-Konflikt. Der .deb-Launcher und
    ``stt-button setup`` nutzen dieses Default. ``install.sh`` (Dev) legt
    stattdessen explizit die venv im Projekt-Root an (``.venv``).

    Das ASR-Modell wird NICHT hier geholt — die Engine laedt es vom
    Hugging-Face-Cache beim ersten Diktat.
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
