"""Tests für den BufferedTranscriber (STUFE 3, TDD-Kette).

Hybrid-Strategie (STUFE 3):
  * Phase A (0 .. whole_buffer_until_s): wachsender Volllpuffer, aber bereits
    mit Zeitstempeln + TimestampCommitter(require_agreement_always=True).
  * Phase B (> whole_buffer_until_s): zustandslose, feste Fenster
    [left_context | chunk | right_context], Dedup über ABSOLUTE Zeitstempel,
    Commit nur nach 2 Agreements (immer).
  * finalize(): ohne neue Bytes ohne Engine-Call; sonst nur uncommitted
    Tail + Links-Kontext, niemals der Vollpuffer.

Alle Funktionstests laufen mit einer Fake-Engine (deterministisch, ohne
Modell-Download). Der echte-Engine-Test ist mit ``@pytest.mark.slow``
markiert und wird ohne lokales Modell übersprungen.
"""
from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest

from stt_button.asr import TranscriptionResult
from stt_button.buffered import BufferedTranscriber
from stt_button.streaming_types import Token, Transcription

RATE = 16000
BYTES_PER_SECOND = RATE * 2


def audio(seconds: float) -> bytes:
    """Erzeugt ``seconds`` Stille in s16le mono."""
    return b"\x00\x00" * int(seconds * RATE)


def corpus(n_words: int, step_s: float = 0.4) -> list[tuple[str, float, float]]:
    """Deterministisches Drehbuch: ein Wort alle ``step_s`` Sekunden.

    Jedes Token ist ein Wortanfang (führendes Leerzeichen ausser beim
    ersten), damit die Wortgrenzen-Regel des Committers prüfbar ist.
    """
    out: list[tuple[str, float, float]] = []
    for i in range(n_words):
        start = i * step_s
        out.append(("Wort" if i == 0 else f" w{i:03d}", start,
                    start + step_s * 0.8))
    return out


class ScriptEngine:
    """Fake-Engine mit festem Drehbuch ABSOLUTER Zeiten.

    Liefert zu jedem Fenster genau die Tokens, deren Start im absoluten
    Zeitbereich des Fensters liegt. Ueberlappende Fenster liefern damit
    ueberlappende Tokens -> prueft die Absolute-Zeitstempel-Entdopplung.
    """

    def __init__(self, script: list[tuple[str, float, float]], *,
                 wrong_first: list[tuple[str, float, float]] | None = None,
                 silent: bool = False) -> None:
        self.script = script
        self.wrong_first = wrong_first
        self.silent = silent
        #: Eintrag pro Decode-Aufruf: (offset_s, audio_s, n_bytes)
        self.calls: list[tuple[float, float, int]] = []
        self.plain_calls: list[int] = []

    # ---------------- Engine-Interface ---------------------------------
    def transcribe_with_timestamps(
        self, data: bytes, window_offset_s: float = 0.0
    ) -> Transcription:
        duration = len(data) / BYTES_PER_SECOND
        self.calls.append((window_offset_s, duration, len(data)))
        if self.silent:
            return Transcription(tokens=[])
        script = self.wrong_first if (
            self.wrong_first is not None and len(self.calls) == 1
        ) else self.script
        lo = window_offset_s - 1e-6
        hi = window_offset_s + duration - 1e-6
        return Transcription(tokens=[
            Token(text=t, start=s, end=e)
            for t, s, e in script
            if lo <= s <= hi
        ])

    def transcribe(self, data: bytes, initial_prompt: str = "") -> TranscriptionResult:
        self.plain_calls.append(len(data))
        return TranscriptionResult(
            text=self.transcribe_with_timestamps(data, 0.0).text.strip()
        )

    # ---------------- Test-Helfer ---------------------------------------
    @property
    def n_calls(self) -> int:
        return len(self.calls)

    def tail_calls(self, n: int) -> list[tuple[float, float, int]]:
        """Die Decode-Fenster ab dem n-ten Aufruf (z.B. ab finalize)."""
        return self.calls[n:]


class ToggleVad:
    """VAD-Attrappe, deren Ergebnis der Test von Hand schaltet."""

    def __init__(self) -> None:
        self.speech = True
        self.n_calls = 0

    def has_speech(self, arr) -> bool:
        self.n_calls += 1
        return self.speech


def drive(t: BufferedTranscriber, seconds: float, step_s: float = 1.0,
          on_step=None) -> TranscriptionResult | None:
    """Füttert ``seconds`` Audio in ``step_s``-Schritten, pro Schritt ein Tick."""
    chunk = audio(step_s)
    last: TranscriptionResult | None = None
    for i in range(int(round(seconds / step_s))):
        t.feed(chunk)
        if on_step is not None:
            on_step(i)
        res = t.maybe_transcribe()
        if res is not None:
            last = res
    return last


def build(script, *, engine_kwargs=None, **kwargs
          ) -> tuple[BufferedTranscriber, ScriptEngine]:
    """ Baut Transcriber + Fake-Engine (engine_kwargs -> ScriptEngine)."""
    eng = ScriptEngine(script, **(engine_kwargs or {}))
    return BufferedTranscriber(eng, **kwargs), eng


# ----------------------------------------------------------------------
# Interface / Defaults
# ----------------------------------------------------------------------
class TestInterface:
    def test_signature_defaults_match_plan(self) -> None:
        """Exakte Defaults aus dem Kimi-Plan (STUFE 3)."""
        sig = inspect.signature(BufferedTranscriber.__init__)
        expected = {
            "interval_s": 1.0,
            "min_audio_s": 3.0,
            "whole_buffer_until_s": 8.0,
            "left_context_s": 8.0,
            "chunk_s": 2.0,
            "right_context_s": 2.0,
            "commit_agreements": 2,
            "max_final_window_s": 18.0,
            "sample_rate": 16000,
            "vad": None,
        }
        for name, default in expected.items():
            assert name in sig.parameters, f"Parameter {name} fehlt"
            assert sig.parameters[name].default == default, (
                f"{name}: Default {sig.parameters[name].default!r} "
                f"!= erwartet {default!r}"
            )

    def test_window_params_are_keyword_only(self) -> None:
        params = inspect.signature(BufferedTranscriber.__init__).parameters
        for name in ("interval_s", "whole_buffer_until_s", "vad"):
            assert params[name].kind is inspect.Parameter.KEYWORD_ONLY

    def test_below_min_audio_returns_none(self) -> None:
        t, eng = build(corpus(50), interval_s=1.0, min_audio_s=3.0)
        t.feed(audio(2.0))
        assert t.maybe_transcribe() is None
        assert eng.n_calls == 0

    def test_reset_clears_state(self) -> None:
        t, eng = build(corpus(60), interval_s=1.0, min_audio_s=3.0)
        drive(t, 16.0)
        assert t.committed_len > 0
        t.reset()
        assert t.audio_seconds == 0.0
        assert t.committed_len == 0
        assert t.last_text == ""

    def test_result_is_full_text_with_committed_len(self) -> None:
        """Der Controller braucht weiter den VOLLEN Text (committed + partial)."""
        t, eng = build(corpus(80), interval_s=1.0, min_audio_s=3.0)
        res = drive(t, 20.0)
        assert res is not None
        assert res.committed_len > 0
        assert res.text.startswith(t.committed_text.strip())
        assert len(res.text) >= res.committed_len
        assert res.committed_len == len(t.committed_text.rstrip())


# ----------------------------------------------------------------------
# Phase A: 0..8s wachsender Volllpuffer, aber mit Zeitstempeln + Agreement
# ----------------------------------------------------------------------
class TestPhaseA:
    def test_early_windows_are_whole_buffer_from_zero(self) -> None:
        """Unterhalb whole_buffer_until_s: Fenster starten bei 0 und sind <= 8s."""
        t, eng = build(corpus(30), interval_s=1.0, min_audio_s=3.0,
                       whole_buffer_until_s=8.0)
        drive(t, 7.0)
        assert eng.n_calls >= 3, "zu wenige Ticks gelaufen"
        assert all(off == pytest.approx(0.0) for off, _, _ in eng.calls)
        assert max(d for _, d, _ in eng.calls) <= 8.0 + 1e-6

    def test_first_window_commits_nothing_second_agreement_commits(self) -> None:
        """Phase A committet erst nach 2 Identischen - von Anfang an."""
        t, eng = build(corpus(60), interval_s=1.0, min_audio_s=3.0)
        committed_progress: list[int] = []
        chunk = audio(1.0)
        for _ in range(12):
            t.feed(chunk)
            res = t.maybe_transcribe()
            if res is not None:
                committed_progress.append(res.committed_len)
        assert committed_progress, "es wurde nie transkribiert"
        assert committed_progress[0] == 0, "erstes Fenster darf nicht committen"
        assert max(committed_progress) > 0, "auch nach Agreements nichts committet"

    def test_hallucination_in_first_window_is_not_frozen(self) -> None:
        """Fenster 1 liefert Muell, Fenster 2 korrekt -> Muell nie committet."""
        script = corpus(40)
        bad = [("OKAY", 0.0, 0.3), (";", 0.3, 0.4)] + script[2:]
        t, eng = build(script, engine_kwargs={"wrong_first": bad},
                       interval_s=1.0, min_audio_s=3.0)
        drive(t, 24.0)
        assert "OKAY" not in t.committed_text


# ----------------------------------------------------------------------
# Phase B: bounded, zustandslose Fenster
# ----------------------------------------------------------------------
class TestPhaseB:
    def test_44s_never_exceeds_window_budget(self) -> None:
        """44s-Simulation: KEIN Fenster ueber left+chunk+right."""
        t, eng = build(corpus(200, step_s=0.25), interval_s=1.0,
                       min_audio_s=3.0, whole_buffer_until_s=8.0,
                       left_context_s=8.0, chunk_s=2.0, right_context_s=2.0)
        drive(t, 44.0)
        budget = 8.0 + 2.0 + 2.0
        assert eng.n_calls > 5, "zu wenige Fenster gelaufen"
        over = [c for c in eng.calls if c[1] > budget + 1e-6]
        assert not over, f"Fenster ueber Budget: {over[:3]}"

    def test_window_starts_at_committed_minus_left_context(self) -> None:
        """Phase-B-Fenster beginnen bei committed_s - left_context (>= 0)."""
        t, eng = build(corpus(200, step_s=0.25), interval_s=1.0,
                       min_audio_s=3.0, whole_buffer_until_s=8.0,
                       left_context_s=8.0, chunk_s=2.0, right_context_s=2.0)
        drive(t, 30.0)
        starts = [off for off, _, _ in eng.calls if off > 0.0]
        assert starts, "kein Phase-B-Fenster mit Links-Kontext gelaufen"
        assert all(s <= t.audio_seconds for s in starts)

    def test_second_call_without_new_audio_is_throttled(self) -> None:
        """Kein neues Audio -> kein weiterer Engine-Call (Intervall-Throttle)."""
        t, eng = build(corpus(60), interval_s=1.0, min_audio_s=3.0)
        drive(t, 16.0)
        n = eng.n_calls
        assert t.maybe_transcribe() is None
        assert eng.n_calls == n

    def test_no_commit_without_right_context(self) -> None:
        """Ohne ausreichenden Rechtskontext wird kein Commit-Horizont erzeugt."""
        t, eng = build(corpus(40), interval_s=1.0, min_audio_s=3.0,
                       whole_buffer_until_s=8.0, chunk_s=2.0,
                       right_context_s=2.0)
        chunk = audio(1.0)
        for _ in range(9):
            t.feed(chunk)
            t.maybe_transcribe()
        # Phase A: alle Fenster starten bei 0 und bleiben <= whole_buffer_until_s.
        assert eng.calls
        for off, dur, _ in eng.calls:
            assert off == pytest.approx(0.0)
            assert dur <= 8.0 + 1e-6

    def test_overlap_dedup_no_duplicated_words(self) -> None:
        """Ueberlappende Fenster duerfen Woerter nicht verdoppeln."""
        t, eng = build(corpus(220, step_s=0.25), interval_s=1.0, min_audio_s=3.0)
        res = drive(t, 48.0)
        words = res.text.split()
        dupes = sorted({w for w in words if words.count(w) > 1})
        assert not dupes, f"duplizierte Woerter: {dupes[:5]}"

    def test_no_gaps_and_monotonic_word_order(self) -> None:
        """Keine verschluckten Woerter in der Mitte, Reihenfolge monoton."""
        t, eng = build(corpus(220, step_s=0.25), interval_s=1.0, min_audio_s=3.0)
        res = drive(t, 48.0)
        words = res.text.split()
        assert words[0] == "Wort"
        idx = [int(w[1:]) for w in words[1:]]
        assert idx == sorted(idx), "Wortreihenfolge nicht monoton"
        assert idx == list(range(idx[0], idx[0] + len(idx))), "Loecke im Text"

    def test_last_word_never_split(self) -> None:
        """Ein Wortanfang-Token am Commit-Rand bleibt im Partial."""
        script = ([("Das", 0.0, 0.3), (" hier", 0.4, 0.7), (" ist", 0.8, 1.1),
                   (" de", 1.2, 1.35), ("utlich", 1.35, 1.6)]
                  + [(" mehr", 2.0, 2.3), (" text", 2.4, 2.7),
                     (" ue", 2.8, 2.95), ("ber", 2.95, 3.2)]
                  + corpus(400, step_s=0.4)[9:])
        t, eng = build(script, interval_s=0.5, min_audio_s=3.0,
                       whole_buffer_until_s=8.0, chunk_s=1.0,
                       right_context_s=2.0)
        drive(t, 16.0, step_s=0.5)
        assert " de " not in f" {t.committed_text} "
        assert " ue " not in f" {t.committed_text} "

    def test_pause_does_not_stall_and_keeps_previous_words(self) -> None:
        """VAD-Pause: Commit-Punkt rueckt, der bisherige Text bleibt erhalten."""
        vad = ToggleVad()
        t, eng = build(corpus(220, step_s=0.4), interval_s=1.0, min_audio_s=3.0,
                       whole_buffer_until_s=8.0, vad=vad)
        drive(t, 12.0)
        before = t.committed_text.strip()
        assert before != ""

        vad.speech = False
        drive(t, 12.0)
        assert t.last_text.strip().startswith(before), "Text vor der Pause verloren"

        vad.speech = True
        res = drive(t, 24.0)
        assert res.text.startswith(before), "Pause hat den committeten Text verdraengt"
        assert len(res.text) > len(before), "nach der Pause nichts mehr erkannt"


# ----------------------------------------------------------------------
# finalize()
# ----------------------------------------------------------------------
class TestFinalize:
    def _streamed(self, seconds: float = 24.0, script=None):
        t, eng = build(script or corpus(240, step_s=0.25), interval_s=1.0,
                       min_audio_s=3.0)
        drive(t, seconds)
        return t, eng

    def test_no_new_bytes_no_engine_call(self) -> None:
        """Unveraenderte Audio -> committed + last_partial, ohne Engine-Call."""
        t, eng = self._streamed(24.0)
        n = eng.n_calls
        res = t.finalize()
        assert eng.n_calls == n, "finalize darf ohne neue Bytes nicht decoden"
        assert res.text == t.last_text
        assert res.committed_len == len(t.committed_text.rstrip())

    def test_new_bytes_decode_only_tail_plus_context(self) -> None:
        """Neue Bytes -> nur uncommitted Tail + Kontext, nie der Vollpuffer."""
        t, eng = self._streamed(24.0)
        committed = t.committed_text.strip()
        t.feed(audio(2.0))
        n = eng.n_calls
        res = t.finalize()
        assert eng.n_calls > n, "finalize muss den neuen Tail dekodieren"
        for off, dur, _ in eng.tail_calls(n):
            assert dur <= 18.0 + 1e-6, f"Finalize-Fenster ueber Budget: {dur}"
        assert res.text.startswith(t.committed_text.strip())
        assert res.committed_len == len(t.committed_text.rstrip())

    def test_late_finalize_decodes_small_slice(self) -> None:
        """Je spaeter finalize, desto kleiner der dekodierte Bereich."""
        t, eng = build(corpus(400, step_s=0.25), interval_s=1.0, min_audio_s=3.0)
        drive(t, 60.0)
        total_s = t.audio_seconds
        n = eng.n_calls
        t.feed(audio(1.0))
        t.finalize()
        biggest = max(d for _, d, _ in eng.tail_calls(n))
        assert biggest < total_s / 2, (
            f"finalize dekodiert {biggest:.1f}s von {total_s:.1f}s -> Vollpuffer"
        )

    def test_catchup_uses_bounded_windows_not_whole_buffer(self) -> None:
        """Committer weit hinterher -> mehrere begrenzte Fenster, nie Vollpuffer."""
        t, eng = build(corpus(400, step_s=0.25), interval_s=1.0,
                       min_audio_s=3.0, max_final_window_s=18.0)
        # Viel Audio auf einmal, kaum Ticks -> Committer stagniert nahe 0s.
        t.feed(audio(70.0))
        t.maybe_transcribe()
        t.feed(audio(1.0))
        n = eng.n_calls
        res = t.finalize()
        calls = eng.tail_calls(n)
        assert calls, "finalize muss den uncommitted Tail dekodieren"
        for off, dur, _ in calls:
            assert dur <= 18.0 + 1e-6, f"Catch-up-Fenster zu gross: {dur}"
        assert len(res.text.split()) > 5, "Catch-up liefert kaum Text"

    def test_silent_tail_is_not_dropped(self) -> None:
        """VAD meldet stumm: finalize darf den Rest nicht kommentlos verwerfen."""
        vad = ToggleVad()
        t, eng = build(corpus(220, step_s=0.4), interval_s=1.0, min_audio_s=3.0,
                       vad=vad)
        drive(t, 20.0)
        vad.speech = False
        t.feed(audio(3.0))
        n = eng.n_calls
        res = t.finalize()
        assert eng.n_calls > n or res.text == t.last_text, (
            "stummer Tail ohne jede Behandlung"
        )
        assert res.text.startswith(t.committed_text.strip()), "Tail verworfen"


# ----------------------------------------------------------------------
# Langsamer End-to-End-Test mit echter Engine (skip ohne Modell)
# ----------------------------------------------------------------------
def _model_available() -> bool:
    root = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    return (root / "hub" / "models--nvidia--parakeet-tdt-0.6b-v3").is_dir()


@pytest.mark.slow
@pytest.mark.skipif(not _model_available(), reason="Parakeet-Modell nicht vorhanden")
def test_real_engine_long_dictate_stays_within_window_budget() -> None:
    """Echte Engine: langes Diktat -> Fenster im Budget, Text wachst."""
    import wave

    from stt_button.asr import ParakeetEngine

    wav = Path(__file__).parent / "fixtures" / "corpus_fix_01.wav"
    if not wav.is_file():
        pytest.skip("Fixture-WAV fehlt")
    with wave.open(str(wav), "rb") as fh:
        raw = fh.readframes(fh.getnframes())
        rate = fh.getframerate()
    if rate != RATE:
        pytest.skip(f"Fixture hat {rate} Hz, erwartet {RATE} Hz")
    while len(raw) // 2 < 44 * RATE:
        raw = raw + raw
    raw = raw[: 44 * RATE * 2]

    class Counting:
        def __init__(self, inner) -> None:
            self.inner = inner
            self.calls: list[tuple[float, float, int]] = []

        def transcribe_with_timestamps(self, data, window_offset_s=0.0):
            self.calls.append((window_offset_s, len(data) / BYTES_PER_SECOND,
                               len(data)))
            return self.inner.transcribe_with_timestamps(data, window_offset_s)

        def transcribe(self, data, initial_prompt=""):
            return self.inner.transcribe(data, initial_prompt=initial_prompt)

    engine = Counting(ParakeetEngine("nvidia/parakeet-tdt-0.6b-v3"))
    t = BufferedTranscriber(engine, interval_s=1.0, min_audio_s=3.0)
    for i in range(0, len(raw), BYTES_PER_SECOND):
        t.feed(raw[i:i + BYTES_PER_SECOND])
        t.maybe_transcribe()
    res = t.finalize()

    budget = 8.0 + 2.0 + 2.0
    assert all(d <= budget + 1e-6 for _, d, _ in engine.calls)
    assert len(res.text.split()) > 5
