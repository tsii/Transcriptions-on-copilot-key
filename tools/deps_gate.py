#!/usr/bin/env python3
"""Dependency gate for the venv-free runtime (plan_no_venv_B, V1).

Probes a target interpreter (default /usr/bin/python3) for:
  * pip availability,
  * importability of the required modules (with --src on PYTHONPATH),
  * version satisfaction against a requirements file,
  * shadow risk from apt-installed numpy/scipy/soundfile/pyaudio
    in dist-packages.

Closed exit-level set (do not extend):
  0 = all good, 1 = pip_missing/deps_missing,
  2 = requirements_missing (file not found), 3 = shadow_risk.

Writes a short report (default .pi/reports/deps_gate.md).
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_PY = "/usr/bin/python3"
DEFAULT_SRC = "/opt/stt-button/src"
DEFAULT_REQ = "/opt/stt-button/packaging/runtime-requirements.txt"
DEFAULT_REPORT = ".pi/reports/deps_gate.md"

REQUIRED_MODULES = ["numpy", "scipy", "soundfile", "pyaudio", "requests"]
SHADOW_MODULES = ["numpy", "scipy", "soundfile", "pyaudio"]

IMPORT_PROBE = "import importlib,sys\nimportlib.import_module(sys.argv[1])"
VERSION_PROBE = (
    "import importlib.metadata,sys\n"
    "print(importlib.metadata.version(sys.argv[1]))"
)
SYSPATH_PROBE = "import sys\nprint(chr(10).join(sys.path))"


def _run(cmd, env=None):
    return subprocess.run(cmd, capture_output=True, text=True,
                          env=env, timeout=60)


def _run_probe(python, code, arg=None, src=None):
    env = None
    if src is not None:
        env = dict(__import__("os").environ)
        old = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(src) + ((":" + old) if old else "")
    cmd = [python, "-c", code]
    if arg is not None:
        cmd.append(arg)
    return _run(cmd, env=env)


def _vtuple(version):
    """Very small version tuple: numeric chunks only."""
    parts = re.split(r"[.\-+]", str(version))
    out = []
    for part in parts:
        out.append(int(part) if part.isdigit() else 0)
    return tuple(out)


def _satisfies(installed, op, wanted):
    i, w = _vtuple(installed), _vtuple(wanted)
    pad = max(len(i), len(w))
    i += (0,) * (pad - len(i))
    w += (0,) * (pad - len(w))
    if op == "==":
        return i == w
    if op == ">=":
        return i >= w
    if op == "<=":
        return i <= w
    if op == "~=":
        return i[:2] == w[:2] and i >= w
    return True  # unknown operator: do not block on it


def _parse_requirements(path):
    """Yield (module, op, version) from a requirements file."""
    reqs = []
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*(==|>=|<=|~=|!=)\s*([^\s;]+)", line)
        if m:
            reqs.append((m.group(1), m.group(2), m.group(3)))
        else:
            m2 = re.match(r"^([A-Za-z0-9_.\-]+)", line)
            if m2:
                reqs.append((m2.group(1), None, None))
    return reqs


def _write_report(path, target, src, req, findings, level, label):
    lines = [
        "# deps_gate report",
        "",
        f"- target interpreter: `{target}`",
        f"- src path: `{src}`",
        f"- requirements: `{req}`",
        "",
        "## findings",
        "",
    ]
    lines += [f"- {f}" for f in findings]
    lines += ["", f"result: `{label}` (exit {level})", ""]
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines))
    print(label)
    for f in findings:
        print(f"  {f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--python", default=DEFAULT_PY,
                    help="target interpreter to probe (default %(default)s)")
    ap.add_argument("--src", default=DEFAULT_SRC,
                    help="src path added to PYTHONPATH during import probes")
    ap.add_argument("--requirements", default=DEFAULT_REQ,
                    help="requirements file for the version comparison")
    ap.add_argument("--report", default=DEFAULT_REPORT,
                    help="path of the markdown report")
    args = ap.parse_args()
    src = Path(args.src)
    req = Path(args.requirements)
    findings = []

    # Exit 2: requirements file not found (the R1/R3 confusion case).
    if not req.is_file():
        label = "requirements_missing"
        _write_report(args.report, args.python, src, req,
                      [f"requirements file not found: {req}"], 2, label)
        return 2

    # Exit 1: pip missing under the target interpreter.
    if _run([args.python, "-m", "pip", "--version"]).returncode != 0:
        label = "pip_missing"
        _write_report(args.report, args.python, src, req,
                      [f"pip not usable via {args.python} -m pip"], 1, label)
        return 1
    findings.append(f"pip present under `{args.python}`")

    # Exit 1: required modules must import under the target interpreter.
    missing = [m for m in REQUIRED_MODULES
               if _run_probe(args.python, IMPORT_PROBE, m, src).returncode != 0]
    if missing:
        label = "deps_missing"
        _write_report(args.report, args.python, src, req,
                      findings + [f"not importable: {', '.join(missing)}"],
                      1, label)
        return 1
    findings.append(
        "all required modules importable: " + ", ".join(REQUIRED_MODULES))

    # Exit 1: version comparison against the requirements file.
    mismatches = []
    for module, op, wanted in _parse_requirements(req):
        if op is None:
            continue
        res = _run_probe(args.python, VERSION_PROBE, module)
        installed = res.stdout.strip() if res.returncode == 0 else None
        if installed is None or not _satisfies(installed, op, wanted):
            mismatches.append(
                f"{module}: found {installed}, want {op}{wanted}")
    if mismatches:
        label = "deps_missing"
        _write_report(args.report, args.python, src, req,
                      findings + [f"version mismatch: {m}" for m in mismatches],
                      1, label)
        return 1
    findings.append("all requirements versions satisfied")

    # Exit 3: shadow risk from dist-packages entries on sys.path.
    shadowed = []
    res = _run_probe(args.python, SYSPATH_PROBE)
    for entry in (res.stdout or "").splitlines():
        if "dist-packages" not in entry:
            continue
        for mod in SHADOW_MODULES:
            base = Path(entry)
            if (base / mod).exists() or (base / (mod + ".py")).exists():
                shadowed.append(f"{mod} in {entry}")
    if shadowed:
        label = "shadow_risk"
        _write_report(args.report, args.python, src, req,
                      findings + sorted(set(shadowed)) +
                      ["apt dist-packages copies shadow the runtime"],
                      3, label)
        return 3

    label = "all_good"
    _write_report(args.report, args.python, src, req, findings, 0, label)
    return 0


if __name__ == "__main__":
    sys.exit(main())
