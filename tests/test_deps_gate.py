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


def test_pip_missing_exit_1(tmp_path):
    """E1b (a): "pip is missing" is simulated by the fake interpreter.

    The old version probed the host's /usr/bin/python3, which asserted a host
    condition: after the operator installed python3-pip (installer step R2) the
    precondition stopped being true and the test failed permanently. Same class
    as E2b - no real dependency on the test environment.
    """
    fake = _make_fake_python(tmp_path, pip_rc=1)
    req = tmp_path / "runtime-requirements.txt"
    req.write_text("numpy==1.0.0\n")
    report = tmp_path / "report.md"
    res = _run_gate("--python", fake, "--src", REPO / "src",
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


# --- V4a-fix: -m entry point must behave like the tools/ shell ---

MOD = "stt_button.deps_gate"


def _run_module_subprocess(*args):
    """Run the gate via `python -m stt_button.deps_gate` in a subprocess."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src") + (":" + env.get("PYTHONPATH", "")
                                               if env.get("PYTHONPATH") else "")
    cmd = [sys.executable, "-m", MOD, *[str(a) for a in args]]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                          cwd=REPO, env=env)


def test_dash_m_requirements_missing_exit_2(tmp_path):
    fake = _make_fake_python(tmp_path)
    report = tmp_path / "m-report.md"
    res = _run_module_subprocess("--python", fake, "--src", REPO / "src",
                                 "--requirements", "/nonexistent",
                                 "--report", report)
    assert res.returncode == 2
    assert "requirements_missing" in res.stdout
    assert report.is_file()


def test_dash_m_pip_missing_exit_1_with_fake_python(tmp_path):
    """E1b (a): the -m entry point must reach the same level as the tools/
    shell, driven by the fake interpreter (pip_rc=1), not by the host."""
    fake = _make_fake_python(tmp_path, pip_rc=1)
    req = tmp_path / "runtime-requirements.txt"
    req.write_text("numpy==1.0.0\n")
    report = tmp_path / "m-report.md"
    res = _run_module_subprocess("--python", str(fake), "--src", REPO / "src",
                                 "--requirements", req, "--report", report)
    assert res.returncode == 1
    assert "pip_missing" in res.stdout
    assert report.is_file()


def test_dash_m_silence_rule_never_exit_0_without_report(tmp_path):
    """Silence rule: a run that writes no report and prints nothing must
    never exit 0 (would be a false green). Provoke it with a nonexistent
    requirements file and a report path in a read-only-ish place is not
    needed — the guard itself must not swallow the exit level."""
    fake = _make_fake_python(tmp_path)
    report = tmp_path / "silent-report.md"
    res = _run_module_subprocess("--python", fake, "--src", REPO / "src",
                                 "--requirements", "/nonexistent",
                                 "--report", report)
    assert res.returncode != 0 or report.is_file()
    if res.returncode != 0:
        assert res.stdout.strip() or report.is_file(), (
            "silent non-zero exit without any report is acceptable only "
            "with output")


def test_dash_m_matches_tools_shell_levels(tmp_path):
    fake = _make_fake_python(tmp_path, pip_rc=1)
    req = tmp_path / "runtime-requirements.txt"
    req.write_text("numpy==1.0.0\n")
    res_mod = _run_module_subprocess(
        "--python", fake, "--src", REPO / "src", "--requirements", req,
        "--report", tmp_path / "m.md")
    res_tool = _run_gate("--python", fake, "--src", REPO / "src",
                         "--requirements", req,
                         "--report", tmp_path / "t.md")
    assert res_mod.returncode == res_tool.returncode == 1


def test_version_mismatch_deps_missing_exit_1(tmp_path):
    fake = _make_fake_python(tmp_path, version="0.9.0")
    req = tmp_path / "runtime-requirements.txt"
    req.write_text("numpy==1.0.0\n")
    report = tmp_path / "report.md"
    res = _run_gate("--python", fake, "--src", REPO / "src",
                    "--requirements", req, "--report", report)
    assert res.returncode == 1
    assert "deps_missing" in res.stdout


# --- E1 (plan_review_sipgate_20261111): pyaudio is not a dependency at all ---
# Audio capture runs via the external `pw-record` binary (src/stt_button/audio.py);
# no module in the package ever imports pyaudio. Requiring it made gate Level 0 -
# and the hard pre-install check of the installer - unreachable on a correct
# system. These tests pin the corrected contract.

def test_pyaudio_missing_is_not_a_gate_failure(tmp_path):
    fake = _make_fake_python(tmp_path, missing="pyaudio")
    req = tmp_path / "runtime-requirements.txt"
    req.write_text("numpy==1.0.0\n")
    report = tmp_path / "report.md"
    res = _run_gate("--python", fake, "--src", REPO / "src",
                    "--requirements", req, "--report", report)
    assert res.returncode == 0, res.stdout
    assert "all_good" in res.stdout


def test_pyaudio_in_dist_packages_is_not_shadow_risk(tmp_path):
    """An apt-provided pyaudio shadows nothing we import: no Level 3."""
    dist = tmp_path / "dist-packages"
    (dist / "pyaudio").mkdir(parents=True)
    fake = _make_fake_python(tmp_path, dist_packages=dist)
    req = tmp_path / "runtime-requirements.txt"
    req.write_text("numpy==1.0.0\n")
    report = tmp_path / "report.md"
    res = _run_gate("--python", fake, "--src", REPO / "src",
                    "--requirements", req, "--report", report)
    assert res.returncode == 0, res.stdout


def test_gate_module_lists_contain_no_pyaudio():
    src = (REPO / "src" / "stt_button" / "deps_gate.py").read_text()
    rows = [line for line in src.splitlines()
            if line.startswith(("REQUIRED_MODULES", "SHADOW_MODULES"))]
    assert rows, "module lists not found in deps_gate.py"
    assert not any("pyaudio" in line for line in rows), rows
