"""Tests for tools/summary_check.py (B11b gate tool, exit levels 0/1/2).

No real network: every HTTP touchpoint is monkeypatched. Without
STT_SUMMARY_REAL the fake counts calls and the real session sees none.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import summary_check  # noqa: E402
from stt_button.summary import ModelUnavailable  # noqa: E402

MODELS = ["my-qwen38-flash-next", "my-other-model"]


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise summary_check.requests.exceptions.HTTPError("http error")


class FakeHTTP:
    """Counts calls; never touches the network."""

    def __init__(self):
        self.get_calls = 0
        self.post_calls = 0
        exceptions = __import__("requests").exceptions
        self.exceptions = exceptions

    def get(self, url, **kwargs):
        self.get_calls += 1
        return FakeResponse({"data": [{"id": m} for m in MODELS]})

    def post(self, url, **kwargs):
        self.post_calls += 1
        return FakeResponse(
            {"choices": [{"message": {"content": "kurze antwort"}}]}
        )


@pytest.fixture()
def cfg():
    from stt_button.config import load_config

    return load_config(None)


@pytest.fixture()
def no_real(monkeypatch):
    monkeypatch.delenv("STT_SUMMARY_REAL", raising=False)


def _patch_all(monkeypatch, fake):
    monkeypatch.setattr(summary_check, "requests", fake)
    import stt_button.summary as summary_mod

    monkeypatch.setattr(summary_mod, "requests", fake)


def test_all_gates_green_exit_0(monkeypatch, cfg, no_real):
    fake = FakeHTTP()
    _patch_all(monkeypatch, fake)
    code, lines = summary_check.check(cfg)
    assert code == 0
    assert cfg.summary_model in " ".join(lines)
    assert fake.get_calls == 1 and fake.post_calls == 1


def test_model_missing_exit_1(monkeypatch, cfg, no_real):
    fake = FakeHTTP()
    fake.get = lambda url, **kw: FakeResponse(
        {"data": [{"id": "unrelated"}]})
    _patch_all(monkeypatch, fake)
    code, _ = summary_check.check(cfg)
    assert code == 1


def test_timeout_exit_1(monkeypatch, cfg, no_real):
    fake = FakeHTTP()

    def slow_post(url, **kw):
        fake.post_calls += 1
        raise fake.exceptions.Timeout("timed out")

    fake.post = slow_post
    _patch_all(monkeypatch, fake)
    code, _ = summary_check.check(cfg)
    assert code == 1


def test_answer_equals_prompt_exit_1(monkeypatch, cfg, no_real):
    fake = FakeHTTP()
    prompt = {"content": summary_check.ROUNDTRIP_PROMPT}

    def echo_post(url, **kw):
        fake.post_calls += 1
        return FakeResponse({"choices": [{"message": prompt}]})

    fake.post = echo_post
    _patch_all(monkeypatch, fake)
    code, _ = summary_check.check(cfg)
    assert code == 1


def test_unreachable_exit_2_no_traceback(monkeypatch, cfg, no_real, capsys):
    fake = FakeHTTP()

    def dead_get(url, **kw):
        raise fake.exceptions.ConnectionError("refused")

    fake.get = dead_get
    _patch_all(monkeypatch, fake)
    code, lines = summary_check.check(cfg)
    assert code == 2
    assert "Traceback" not in "\n".join(lines)


def test_main_exit_2_on_config_error(monkeypatch, tmp_path, no_real, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text("summary_timeout_s = 0\n")
    monkeypatch.setattr(sys, "argv", ["summary_check.py", "--config", str(bad)])
    with pytest.raises(SystemExit) as exc:
        summary_check.main()
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "Traceback" not in out


def test_no_real_flag_fake_counts_real_session_zero(
        monkeypatch, cfg, no_real):
    import requests as real_requests

    fake = FakeHTTP()
    _patch_all(monkeypatch, fake)
    code, _ = summary_check.check(cfg)
    assert code == 0
    assert fake.get_calls == 1 and fake.post_calls == 1
    # the real session object is untouched: requests.get/post still the
    # originals, so zero real HTTP calls happened
    assert fake.get is not real_requests.get
    assert fake.post is not real_requests.post


def test_list_models_marks_active(monkeypatch, cfg, no_real, capsys):
    fake = FakeHTTP()
    _patch_all(monkeypatch, fake)
    lines = summary_check.list_models(cfg, session=fake)
    joined = "\n".join(lines)
    assert cfg.summary_model in joined
    assert "*" in joined
