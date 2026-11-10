"""Tests for tools/deps_gate.py (plan_no_venv_B, V1).

The gate is exercised as a subprocess so its exit level is observable.
All fakes live in tmp_path; the report path is redirected via --report.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "deps_gate.py"
PY3 = "/usr/bin/python3"

FAKE_PY_TEMPLATE = """#!/bin/sh
code="$2"
last=""
for last in "$@"; do :; done
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then exit {pip_rc}; fi
case "$code" in
  *"sys.path"*) cat "{syspath_file}"; exit 0;;
  *"importlib.metadata"*) echo "{version}"; exit 0;;
  *import_module*)
    case " {missing} " in *" $last "*) exit 1;; esac
    exit 0;;
esac
exit 1
"""


def _make_fake_python(tmp_path, pip_rc=0, missing="", version="1.0.0",
                      dist_packages=None):
    """Create a fake target interpreter.

    pip_rc: exit code for the `-m pip --version` probe.
    missing: space-separated module names that fail import.
    version: version echoed for every importlib.metadata probe.
    dist_packages: dir printed as the only sys.path entry (may contain
    numpy etc. subdirectories to simulate an apt shadow install).
    """
    syspath_file = tmp_path / "syspath.txt"
    syspath_file.write_text((str(dist_packages) if dist_packages
                             else str(tmp_path / "no-dist-packages")) + "\n")
    fake = tmp_path / "fakepython"
    fake.write_text(FAKE_PY_TEMPLATE.format(
        pip_rc=pip_rc, missing=missing, version=version,
        syspath_file=syspath_file))
    fake.chmod(0o755)
    return fake


def _run_gate(*args):
    assert TOOL.is_file(), "tools/deps_gate.py missing"
    cmd = [sys.executable, str(TOOL), *[str(a) for a in args]]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def test_real_system_pip_missing_exit_1(tmp_path):
    req = tmp_path / "runtime-requirements.txt"
    req.write_text("numpy==1.0.0\n")
    report = tmp_path / "report.md"
    res = _run_gate("--python", PY3, "--src", REPO / "src",
                    "--requirements", req, "--report", report)
    assert res.returncode == 1
    assert "pip_missing" in res.stdout
    assert report.is_file()
    assert "pip_missing" in report.read_text()


def test_requirements_missing_exit_2_not_deps_missing(tmp_path):
    fake = _make_fake_python(tmp_path)
    report = tmp_path / "report.md"
    res = _run_gate("--python", fake, "--src", REPO / "src",
                    "--requirements", "/nonexistent", "--report", report)
    assert res.returncode == 2
    assert "requirements_missing" in res.stdout
    assert "deps_missing" not in res.stdout


def test_fake_complete_environment_exit_0(tmp_path):
    fake = _make_fake_python(tmp_path)
    req = tmp_path / "runtime-requirements.txt"
    req.write_text("numpy==1.0.0\nrequests>=1.0\n")
    report = tmp_path / "report.md"
    res = _run_gate("--python", fake, "--src", REPO / "src",
                    "--requirements", req, "--report", report)
    assert res.returncode == 0
    assert report.is_file()
    assert "all_good" in report.read_text()


def test_apt_numpy_in_dist_packages_exit_3(tmp_path):
    dist = tmp_path / "dist-packages"
    (dist / "numpy").mkdir(parents=True)
    fake = _make_fake_python(tmp_path, dist_packages=dist)
    req = tmp_path / "runtime-requirements.txt"
    req.write_text("numpy==1.0.0\n")
    report = tmp_path / "report.md"
    res = _run_gate("--python", fake, "--src", REPO / "src",
                    "--requirements", req, "--report", report)
    assert res.returncode == 3
    assert "shadow_risk" in res.stdout
    assert "shadow_risk" in report.read_text()


def test_missing_module_deps_missing_exit_1(tmp_path):
    fake = _make_fake_python(tmp_path, missing="scipy")
    req = tmp_path / "runtime-requirements.txt"
    req.write_text("numpy==1.0.0\n")
    report = tmp_path / "report.md"
    res = _run_gate("--python", fake, "--src", REPO / "src",
                    "--requirements", req, "--report", report)
    assert res.returncode == 1
    assert "deps_missing" in res.stdout


# --- V4a: gate lives in the package, tools/ is a thin shell ---

def _run_module_main(fake_py, tmp_path, requirements):
    """Call stt_button.deps_gate.main() in-process with patched argv."""
    import stt_button.deps_gate as dg
    argv = ["deps_gate", "--python", str(fake_py), "--src", str(REPO / "src"),
            "--requirements", str(requirements),
            "--report", str(tmp_path / "module-report.md")]
    old = sys.argv
    sys.argv = argv
    try:
        return dg.main()
    finally:
        sys.argv = old


def test_package_module_import_and_call(tmp_path):
    fake = _make_fake_python(tmp_path)
    req = tmp_path / "runtime-requirements.txt"
    req.write_text("numpy==1.0.0\n")
    assert _run_module_main(fake, tmp_path, req) == 0
    assert (tmp_path / "module-report.md").read_text().find("all_good") >= 0


def test_module_requirements_missing_exit_2(tmp_path):
    fake = _make_fake_python(tmp_path)
    assert _run_module_main(fake, tmp_path, "/nonexistent") == 2


def test_shell_matches_module_exit_levels(tmp_path):
    fake = _make_fake_python(tmp_path, pip_rc=1)
    req = tmp_path / "runtime-requirements.txt"
    req.write_text("numpy==1.0.0\n")
    # pip_missing -> 1 via both paths
    res = _run_gate("--python", fake, "--src", REPO / "src",
                    "--requirements", req, "--report", tmp_path / "r.md")
    assert res.returncode == 1
    assert _run_module_main(fake, tmp_path, req) == 1
    # requirements_missing -> 2 via both paths
    res = _run_gate("--python", fake, "--src", REPO / "src",
                    "--requirements", "/nonexistent",
                    "--report", tmp_path / "r.md")
    assert res.returncode == 2
    assert _run_module_main(fake, tmp_path, "/nonexistent") == 2


def test_tools_path_contains_no_measurement_logic():
    text = TOOL.read_text()
    for marker in ("subprocess", "_parse_requirements", "argparse",
                   "IMPORT_PROBE", "SHADOW_MODULES", "_run_probe"):
        assert marker not in text, f"tools/deps_gate.py still contains {marker}"
    assert "from stt_button.deps_gate import main" in text


def test_version_mismatch_deps_missing_exit_1(tmp_path):
    fake = _make_fake_python(tmp_path, version="0.9.0")
    req = tmp_path / "runtime-requirements.txt"
    req.write_text("numpy==1.0.0\n")
    report = tmp_path / "report.md"
    res = _run_gate("--python", fake, "--src", REPO / "src",
                    "--requirements", req, "--report", report)
    assert res.returncode == 1
    assert "deps_missing" in res.stdout
