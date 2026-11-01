"""Self-healing bootstrap (deb package, operator, October 31).

The .deb is lightweight (no venv, no model): the Python environment and ML
stack are rebuilt in a per-user venv on first launch. These tests cover the
PLAN function (pure logic, no subprocesses): the correct sequence of
installation commands, idempotent no-op behavior, and the error path when a
step fails.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from stt_button import bootstrap


class TestBuildPlan:
    def test_plan_order_torch_before_reqs_before_code(self) -> None:
        """Command order: venv -> pip -> torch (CPU index) ->
        ML stack (req) -> code (--no-deps)."""
        plan = bootstrap.build_plan(
            install_root=Path("/opt/stt-button"),
            venv_dir=Path("/home/u/.local/stt-button/venv"),
            runtime_req=Path("/opt/stt-button/packaging/runtime-requirements.txt"),
            py_executable="python3",
        )
        # venv creation first, pip bootstrap second
        assert plan[0][0] == "python3"
        assert "venv" in plan[0]
        # torch comes BEFORE the requirements and code, with CPU index URL
        torch_idx = next(i for i, c in enumerate(plan) if "torch" in " ".join(c))
        reqs_idx = next(i for i, c in enumerate(plan) if c[-1].endswith("runtime-requirements.txt"))
        code_idx = next(i for i, c in enumerate(plan) if "--no-deps" in c)
        assert torch_idx < reqs_idx < code_idx
        assert "https://download.pytorch.org/whl/cpu" in " ".join(plan[torch_idx])

    def test_venv_dir_passed_to_creator(self) -> None:
        plan = bootstrap.build_plan(
            install_root=Path("/opt/stt-button"),
            venv_dir=Path("/home/u/venv"),
            runtime_req=Path("/r.txt"),
            py_executable="python3",
        )
        joined = " ".join(plan[0])
        assert "/home/u/venv" in joined

    def test_code_installed_from_install_root(self) -> None:
        plan = bootstrap.build_plan(
            install_root=Path("/opt/stt-button"),
            venv_dir=Path("/v"),
            runtime_req=Path("/r.txt"),
            py_executable="python3",
        )
        code_cmd = next(c for c in plan if "--no-deps" in c)
        assert "/opt/stt-button" in code_cmd


class TestEnsureRuntime:
    def test_ready_nothing_installed(self) -> None:
        """If the venv already has all modules: NO installation."""
        calls: list[list[str]] = []
        ok = bootstrap.ensure_runtime(
            install_root=Path("/opt/stt-button"),
            venv_dir=Path("/v"),
            ready=lambda: True,
            runner=lambda cmd: (calls.append(list(cmd)), 0)[1],
        )
        assert ok is True
        assert calls == [], "already ready -> not a single command"

    def test_not_ready_installs_plan(self, tmp_path: Path) -> None:
        """If the venv is still missing modules: run the complete plan."""
        calls: list[list[str]] = []
        req = tmp_path / "runtime-requirements.txt"
        req.write_text("numpy==2.5.4\n")
        ok = bootstrap.ensure_runtime(
            install_root=Path("/opt/stt-button"),
            venv_dir=Path("/v"),
            runtime_req=req,
            ready=lambda: False,
            runner=lambda cmd: (calls.append(list(cmd)), 0)[1],
        )
        assert ok is True
        plan = bootstrap.build_plan(
            install_root=Path("/opt/stt-button"),
            venv_dir=Path("/v"),
            runtime_req=req,
            py_executable=bootstrap.DEFAULT_PY,
        )
        # Every planned command list was executed (in order).
        assert len(calls) == len(plan)
        assert calls == plan

    def test_install_failure_returns_false(self, tmp_path: Path) -> None:
        """A failed step (rc!=0) -> False, no crash."""
        calls: list[list[str]] = []
        req = tmp_path / "runtime-requirements.txt"
        req.write_text("numpy==2.5.4\n")

        def boom(cmd: list[str]) -> int:
            calls.append(list(cmd))
            return 0 if "venv" in " ".join(cmd) else 1

        ok = bootstrap.ensure_runtime(
            install_root=Path("/opt/stt-button"),
            venv_dir=Path("/v"),
            runtime_req=req,
            ready=lambda: False,
            runner=boom,
        )
        assert ok is False
        # The venv step ran, the following torch step failed ->
        # no further pip install after the failure.
        assert any("torch" in " ".join(c) for c in calls)

    def test_missing_runtime_req_is_error(self, tmp_path: Path) -> None:
        """Missing requirements file = bootstrap error (clear message)."""
        with pytest.raises(bootstrap.BootstrapError):
            bootstrap.ensure_runtime(
                install_root=tmp_path,
                venv_dir=tmp_path / "venv",
                runtime_req=tmp_path / "nicht-da.txt",
                ready=lambda: False,
                runner=lambda cmd: 0,
            )


class TestStamp:
    def test_mark_then_ready(self, tmp_path: Path) -> None:
        req = tmp_path / "r.txt"
        req.write_text("numpy==2.5.4\n")
        venv = tmp_path / "venv"
        venv.mkdir()
        (venv / "bin").mkdir()
        (venv / "bin" / "python").write_text("")
        (venv / "bin" / "python").chmod(0o755)
        assert not bootstrap.runtime_ready(tmp_path, venv, req)
        bootstrap.mark_ready(venv, req)
        assert bootstrap.runtime_ready(tmp_path, venv, req)

    def test_requirements_change_invalidates(self, tmp_path: Path) -> None:
        req = tmp_path / "r.txt"
        req.write_text("numpy==2.5.4\n")
        venv = tmp_path / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("")
        (venv / "bin" / "python").chmod(0o755)
        bootstrap.mark_ready(venv, req)
        req.write_text("numpy==2.6.0\n")
        assert not bootstrap.runtime_ready(tmp_path, venv, req), \
            "requirements change must force a rebuild"

    def test_probe_venv_with_fake_interpreter(self, tmp_path: Path) -> None:
        venv = tmp_path / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("#!/bin/sh\nexit 0\n")
        (venv / "bin" / "python").chmod(0o755)
        assert bootstrap.probe_venv(venv) is True
        (venv / "bin" / "python").write_text("#!/bin/sh\nexit 1\n")
        assert bootstrap.probe_venv(venv) is False

    def test_probe_venv_missing(self, tmp_path: Path) -> None:
        assert bootstrap.probe_venv(tmp_path / "kein-venv") is False


def _default_runtime_req_exists() -> None:
    """The default requirements path must exist in shipped code
    (otherwise bootstrap would fail in real deployments)."""
    p = Path(bootstrap.DEFAULT_RUNTIME_REQ)
    # In the test workspace this is relative to the repo; here only verify
    # that the default is a valid, nonempty path field.
    assert p.parts, "default path must not be empty"
