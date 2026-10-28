"""Tests für den TimestampCommitter (Schritt 3).

Kernregel: Ein einzelnes Wortanfang-Token am Ende (z.B. " de" als Beginn von
"deutlich") ist unvollständig und darf NICHT committet werden. Vollständige
Wörter (Wortanfang + ggf. Folge-Subwords) werden committet.
"""
from __future__ import annotations

import pytest

from stt_button.streaming_types import Token, Transcription
from stt_button.committer import TimestampCommitter


def _tokens(pairs: list[tuple[str, float, float]]) -> list[Token]:
    return [Token(text=t, start=s, end=e) for t, s, e in pairs]


class TestCommit:
    def test_complete_words_are_committed(self) -> None:
        c = TimestampCommitter()
        # "Das" + "muss" + "deutlich" (Wortanfang "de" + Folge "utlich")
        tr = Transcription(_tokens([
            ("Das", 0.0, 0.2),
            (" muss", 0.2, 0.4),
            (" de", 0.4, 0.5),
            ("utlich", 0.5, 0.7),
        ]))
        c.update(tr, commit_horizon=0.7)  # erstes Fenster: Kandidat
        commit, partial = c.update(tr, commit_horizon=0.7)  # zweites: committen
        assert "deutlich" in commit

    def test_incomplete_word_start_not_committed(self) -> None:
        c = TimestampCommitter()
        # Satz endet mit " de" (Wortanfang von "deutlich", noch unfertig)
        tr = Transcription(_tokens([
            ("So", 0.0, 0.3),
            (" jetzt", 0.3, 0.6),
            (" de", 0.6, 0.8),   # unvollständiges Wort
        ]))
        commit, partial = c.update(tr, commit_horizon=0.9)
        # "de" darf nicht im Commit sein (ist Wortanfang, unvollständig)
        assert "de" not in commit

    def test_word_crossing_horizon_stays_partial(self) -> None:
        c = TimestampCommitter()
        tr = Transcription(_tokens([
            ("So", 0.0, 0.16),
            (" jetzt", 0.16, 0.40),
            (" weit", 0.40, 0.88),  # endet nach horizon=0.6
        ]))
        commit, partial = c.update(tr, commit_horizon=0.6)
        # "weit" ist nicht im Commit (endet nach Horizont)
        assert "weit" not in commit

    def test_empty_does_not_crash(self) -> None:
        c = TimestampCommitter()
        commit, partial = c.update(Transcription([]), commit_horizon=1.0)
        assert commit == ""
        assert partial == ""

    def test_committed_s_grows_monotonically(self) -> None:
        c = TimestampCommitter()
        # Mehrere Fenster, committed_s darf nie schrumpfen
        seqs = [
            [("a", 0.0, 0.1), (" b", 0.1, 0.2)],
            [("a", 0.0, 0.1), (" b", 0.1, 0.2), (" c", 0.2, 0.3)],
        ]
        c.update(Transcription(_tokens(seqs[0])), commit_horizon=0.2)
        before = c.committed_s
        c.update(Transcription(_tokens(seqs[1])), commit_horizon=0.3)
        assert c.committed_s >= before


class TestPartial:
    def test_incomplete_word_is_in_partial(self) -> None:
        c = TimestampCommitter()
        tr = Transcription(_tokens([
            ("So", 0.0, 0.3),
            (" jetzt", 0.3, 0.6),
            (" de", 0.6, 0.8),   # unvollständig
        ]))
        commit, partial = c.update(tr, commit_horizon=0.9)
        assert "de" in partial

    def test_next_window_commits_previously_partial_word(self) -> None:
        c = TimestampCommitter()
        # Erstes Fenster: "de" unvollständig (Wortanfang allein)
        tr1 = Transcription(_tokens([
            ("Das", 0.0, 0.2),
            (" muss", 0.2, 0.4),
            (" de", 0.4, 0.5),
        ]))
        c.update(tr1, commit_horizon=0.5)
        # Zweites Fenster: "deutlich" jetzt komplett (de + utlich)
        tr2 = Transcription(_tokens([
            ("Das", 0.0, 0.2),
            (" muss", 0.2, 0.4),
            (" de", 0.4, 0.5),
            ("utlich", 0.5, 0.7),
        ]))
        c.update(tr2, commit_horizon=0.7)  # erstes Auftauchen
        commit, partial = c.update(tr2, commit_horizon=0.7)  # Agreement
        assert "deutlich" in commit

class TestAgreement:
    def test_no_commit_without_agreement(self) -> None:
        """Fix 2 (A1): ein einzelnes Fenster darf noch NICHT committen."""
        c = TimestampCommitter(commit_agreements=2)
        # Fenster 1 liefert "OKAY;"-artige Halluzination
        tr1 = Transcription(_tokens([
            ("OKAY", 0.0, 0.3),
            (";", 0.3, 0.3),
            (" das", 0.3, 0.6),
        ]))
        commit, partial = c.update(tr1, commit_horizon=0.6)
        # Nichts darf committet werden (erst nach 2 Uebereinstimmungen)
        assert commit == ""

    def test_agreement_commits_after_two_matches(self) -> None:
        c = TimestampCommitter(commit_agreements=2)
        tokens = _tokens([
            ("Und", 0.0, 0.2),
            (" geht", 0.2, 0.4),
            (" das", 0.4, 0.6),
            (" jetzt", 0.6, 0.8),
        ])
        c.update(Transcription(tokens), commit_horizon=0.6)
        # Zweites identisches Fenster -> jetzt committen
        commit, partial = c.update(Transcription(tokens), commit_horizon=0.6)
        assert commit.strip() != ""
        assert "Und" in commit

    def test_agreement_corrects_first_window_hallucination(self) -> None:
        c = TimestampCommitter(commit_agreements=2)
        # Fenster 1: Halluzination "OKAY;"
        tr1 = Transcription(_tokens([("OKAY", 0.0, 0.3), (";", 0.3, 0.3)]))
        c.update(tr1, commit_horizon=0.3)
        # Fenster 2 (mehr Kontext): korrekt "Und geht"
        tr2 = Transcription(_tokens([("Und", 0.0, 0.3), (" geht", 0.3, 0.6)]))
        commit, partial = c.update(tr2, commit_horizon=0.6)
        # Die Korrektur gewinnt
        assert "OKAY" not in commit


class TestRequireAgreementAlways:
    """STUFE 3: require_agreement_always erzwingt 2 Agreements bei JEDEM Commit."""

    def test_windowed_behaviour_unchanged_by_default(self) -> None:
        """Default False: ab dem 2. Commit wird direkt committet (wie bisher)."""
        c = TimestampCommitter(commit_agreements=2)
        seg1 = _tokens([("Ein", 0.0, 0.3), (" Satz", 0.3, 0.6),
                        (" mit", 0.6, 0.9), (" Worten", 0.9, 1.2)])
        c.update(Transcription(seg1), commit_horizon=0.9)
        c.update(Transcription(seg1), commit_horizon=0.9)  # 1. Commit (Agreement)
        assert c.committed_s > 0.0
        # Zweites Segment: bisheriges Verhalten = Direkt-Commit im ersten Fenster
        seg2 = seg1 + [Token(" mehr", 1.2, 1.5)]
        commit, partial = c.update(Transcription(seg2), commit_horizon=1.2)
        assert commit.strip() != ""

    def test_always_requires_two_matches_for_every_commit(self) -> None:
        """Mit require_agreement_always darf NIE ein einzelnes Fenster committen."""
        c = TimestampCommitter(commit_agreements=2, require_agreement_always=True)
        seg1 = _tokens([("Ein", 0.0, 0.3), (" Satz", 0.3, 0.6),
                        (" mit", 0.6, 0.9), (" Worten", 0.9, 1.2)])
        c.update(Transcription(seg1), commit_horizon=0.9)
        c.update(Transcription(seg1), commit_horizon=0.9)  # 1. Commit
        assert c.committed_s > 0.0
        before_text = c.committed_text
        seg2 = seg1 + [Token(" mehr", 1.2, 1.5)]
        commit1, _ = c.update(Transcription(seg2), commit_horizon=1.2)
        assert commit1 == "", "einzelnes Fenster darf nicht committen"
        assert c.committed_text == before_text
        commit2, _ = c.update(Transcription(seg2), commit_horizon=1.2)
        assert commit2.strip() != "", "nach 2 Agreements muss committet werden"
        # Mit grossem Horizont kommt das naechste Wort dazu (wieder 2 Agreements).
        for _ in range(2):
            c.update(Transcription(seg2), commit_horizon=1.5)
        assert "Worten" in c.committed_text

    def test_committed_s_monotonic_with_always(self) -> None:
        """committed_s darf auch mit Agreement-Zwang nie schrumpfen."""
        c = TimestampCommitter(commit_agreements=2, require_agreement_always=True)
        seg = _tokens([("a", 0.0, 0.2), (" b", 0.2, 0.4), (" c", 0.4, 0.6)])
        for _ in range(4):
            c.update(Transcription(seg), commit_horizon=0.6)
        first = c.committed_s
        assert first > 0.0
        longer = seg + [Token(" d", 0.6, 0.8)]
        for _ in range(4):
            c.update(Transcription(longer), commit_horizon=0.8)
        assert c.committed_s >= first

    def test_no_commit_without_right_context_candidate(self) -> None:
        """Kein Commit, wenn der Kandidat noch nicht zweimal identical war."""
        c = TimestampCommitter(commit_agreements=2, require_agreement_always=True)
        tr = Transcription(_tokens([("Das", 0.0, 0.3), (" ist", 0.3, 0.6),
                                    (" ein", 0.6, 0.9), (" Test", 0.9, 1.2)]))
        commit, _ = c.update(tr, commit_horizon=1.2)
        assert commit == ""
        assert c.committed_s == 0.0

    def test_changed_candidate_resets_agreement(self) -> None:
        """Aendert sich der Kandidat, beginnt die Zaehlung von vorn."""
        c = TimestampCommitter(commit_agreements=2, require_agreement_always=True)
        common = [("a", 0.0, 0.2), (" b", 0.3, 0.5), (" c", 0.6, 0.8),
                  (" d", 0.9, 1.1)]
        var_a = common + [(" schnell", 1.2, 1.4), ("erei", 1.4, 1.6)]
        var_b = common + [(" langsam", 1.2, 1.4), ("er", 1.4, 1.6)]

        # 1. Commit nach zwei Identischen: "a b c" (" d" bleibt Wortgrenze).
        assert c.update(Transcription(_tokens(var_a)), 1.1)[0] == ""
        commit = c.update(Transcription(_tokens(var_a)), 1.1)[0]
        assert commit == "a b c"
        before = c.committed_text

        # Naechster Kandidat taucht einmal auf ... (Kandidat A)
        assert c.update(Transcription(_tokens(var_a)), 1.6)[0] == ""
        # ... wird dann durch Kandidaten B verdraengt -> Zaehlung startet neu.
        assert c.update(Transcription(_tokens(var_b)), 1.6)[0] == ""
        assert c.committed_text == before
        # Erst zwei Identische von B committen.
        commit = c.update(Transcription(_tokens(var_b)), 1.6)[0]
        assert commit.strip() != ""
        assert "langsam" in commit


class TestWordBoundaryEdgeCases:
    def test_two_single_token_words_commits_first(self) -> None:
        """P1-Fix: 'das jetzt' (2 Einzelwörter) -> 'das' committet, 'jetzt' partial."""
        c = TimestampCommitter()
        tr = Transcription(_tokens([
            (" das", 0.0, 0.4),
            (" jetzt", 0.4, 0.8),
        ]))
        c.update(tr, commit_horizon=0.8)
        commit, partial = c.update(tr, commit_horizon=0.8)  # Agreement
        assert "das" in commit
        assert "jetzt" not in commit

    def test_committed_s_advances_over_windows(self) -> None:
        """P2-Fix: committed_s muss über mehrere Fenster vorrücken, nicht stagnieren."""
        c = TimestampCommitter(commit_agreements=2)
        seg1 = _tokens([("Ein", 0.0, 0.3), (" Satz", 0.3, 0.6), (" mit", 0.6, 0.9), (" Worten", 0.9, 1.2)])
        c.update(Transcription(seg1), commit_horizon=0.9)
        c.update(Transcription(seg1), commit_horizon=0.9)
        assert c.committed_s > 0.0
        before = c.committed_s
        seg2 = _tokens([("Ein", 0.0, 0.3), (" Satz", 0.3, 0.6), (" mit", 0.6, 0.9), (" Worten", 0.9, 1.2), (" mehr", 1.2, 1.5)])
        c.update(Transcription(seg2), commit_horizon=1.2)
        c.update(Transcription(seg2), commit_horizon=1.2)
        assert c.committed_s >= before
