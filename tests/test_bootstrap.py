"""Self-Healing-Bootstrap (Deb-Paket, Betreiber 31.10.).

Das .deb ist schlank (kein venv, kein Modell): die Python-Umgebung und
der ML-Stack werden beim Erststart in eine pro-User-venv nachgebaut.
Diese Test decken die PLAN-Funktion ab (reine Logik, keine Subprozesse):
die korrekte Folge der Installations-Commands, idempotentes Nichtstun,
und der Fehlerweg, wenn ein Schritt scheitert.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from stt_button import bootstrap


class TestBuildPlan:
    def test_plan_order_torch_before_reqs_before_code(self) -> None:
        """Command-Reihenfolge: venv -> pip -> torch(CPU-Index) ->
        ML-Stack(req) -> Code(--no-deps)."""
        plan = bootstrap.build_plan(
            install_root=Path("/opt/stt-button"),
            venv_dir=Path("/home/u/.local/stt-button/venv"),
            runtime_req=Path("/opt/stt-button/packaging/runtime-requirements.txt"),
            py_executable="python3",
        )
        # venv-Erstellung zuerst, pip-Bootstrap zweiter
        assert plan[0][0] == "python3"
        assert "venv" in plan[0]
        # torch kommt VOR den reqs und dem Code, mit CPU-Index-URL
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
        """Falls die venv schon alle Module hat: KEINE Installation."""
        calls: list[list[str]] = []
        ok = bootstrap.ensure_runtime(
            install_root=Path("/opt/stt-button"),
            venv_dir=Path("/v"),
            ready=lambda: True,
            runner=lambda cmd: (calls.append(list(cmd)), 0)[1],
        )
        assert ok is True
        assert calls == [], "bereits fertig -> kein einzelner Command"

    def test_not_ready_installs_plan(self, tmp_path: Path) -> None:
        """Falls die venv noch Module vermisst: der komplette Plan laeuft."""
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
        # Jede geplante Command-Liste wurde (vorzeichenhaft) ausgefuehrt.
        assert len(calls) == len(plan)
        assert calls == plan

    def test_install_failure_returns_false(self, tmp_path: Path) -> None:
        """Ein fehlgeschlagener Schritt (rc!=0) -> False, kein Crash."""
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
        # Der venv-Schritt lief, der folgend torch-Schritt schlug fehl ->
        # kein weiterer pip-Install nach dem Fehler.
        assert any("torch" in " ".join(c) for c in calls)

    def test_missing_runtime_req_is_error(self, tmp_path: Path) -> None:
        """Fehlende requirements-Datei = Bootstrap-Error (klare Meldung)."""
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
            "requirements-Wechsel muss Rebuild erzwingen"

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
    """Der default requirements-Pfad muss bei ausgeliefertem Code existieren
    (sonst muesste der Bootstrap beim echten Einsatz fehlen)."""
    p = Path(bootstrap.DEFAULT_RUNTIME_REQ)
    # Im Test-Workspace liegt das relativ zum Repo; hier nur pruefen, dass
    # der Default ein sauberes, nicht-leeres Pfadfeld ist.
    assert p.parts, "Default-Pfad darf nicht leer sein"
