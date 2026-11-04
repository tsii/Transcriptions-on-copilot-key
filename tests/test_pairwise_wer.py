"""B7: tools/pairwise_wer.py (pure helpers + orchestration with fake engines)."""
from __future__ import annotations

import importlib.util
import json
import wave
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "tools" / "pairwise_wer.py"
_spec = importlib.util.spec_from_file_location("pairwise_wer", _PATH)
pw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pw)

from stt_button.asr import TranscriptionResult  # noqa: E402


def _wav(path: Path, seconds: float) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))
    return path


class Fake:
    def __init__(self, text):
        self.text = text
        self.released = False

    def load(self):
        pass

    def transcribe(self, data, initial_prompt=""):
        return TranscriptionResult(text=self.text)


def test_normalize_strips_case_and_punctuation() -> None:
    assert pw.normalize("Hallo, Welt!  Wie GEHT's?") == ["hallo", "welt", "wie", "geht's"]


def test_wer_basic() -> None:
    assert pw.wer(["a", "b", "c"], ["a", "b", "c"]) == 0.0
    assert pw.wer(["a", "b", "c", "d"], ["a", "x", "c"]) == pytest.approx(0.5)
    assert pw.wer([], []) == 0.0
    assert pw.wer([], ["a"]) == 1.0


def test_select_clips_deterministic_10_to_60s(tmp_path) -> None:
    _wav(tmp_path / "b.wav", 12.0)
    _wav(tmp_path / "a.wav", 5.0)
    _wav(tmp_path / "c.wav", 61.0)
    _wav(tmp_path / "d.wav", 30.0)
    assert [p.name for p in pw.select_clips(tmp_path, limit=40)] == ["b.wav", "d.wav"]
    assert [p.name for p in pw.select_clips(tmp_path, limit=1)] == ["b.wav"]


def test_run_sequential_both_directions_and_top5(tmp_path) -> None:
    clips = [_wav(tmp_path / f"{i}.wav", 11.0) for i in range(6)]
    report = pw.run(clips, ref_factory=lambda: Fake("eins zwei drei vier"),
                    cand_factory=lambda: Fake("eins zwei drei"))
    assert report["summary"]["mean_wer_cand_vs_ref"] == pytest.approx(0.25)
    assert report["summary"]["mean_wer_ref_vs_cand"] == pytest.approx(1 / 3)
    assert len(report["clips"]) == 6
    assert len(report["manual_inspection"]) == 5
    assert report["verdict"] in ("viable_opt_in", "experimental")
    json.dumps(report)


def test_verdict_rule() -> None:
    assert pw.verdict([0.1, 0.15]) == "viable_opt_in"
    assert pw.verdict([0.2, 0.2]) == "experimental"  # mean > 15 %
    assert pw.verdict([0.0, 0.0, 0.6]) == "experimental"  # one clip > 50 %


def test_markdown_report_lists_inspection(tmp_path) -> None:
    clips = [_wav(tmp_path / f"{i}.wav", 11.0) for i in range(2)]
    report = pw.run(clips, ref_factory=lambda: Fake("a b"), cand_factory=lambda: Fake("a c"))
    md = pw.to_markdown(report)
    assert "Manual inspection" in md and "0.wav" in md
