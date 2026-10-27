"""TimestampCommitter: entscheidet anhand von Token-Zeitstempeln, was
endgültig committet (nie mehr revidiert) und was Partial (korrigierbar) ist.

Kernidee aus der Kimi-Recherche: Nur Tokens, deren ``end`` vollständig vor
dem Commit-Horizont liegt UND die bis zur letzten Wortgrenze reichen, werden
committet. Fenster-Overlaps werden über Zeitstempel dedupliziert.
"""
from __future__ import annotations

from .streaming_types import Token, Transcription

FRAME_S = 0.08  # TDT: hop 160 @16kHz * subsampling 8


class TimestampCommitter:
    """Verwaltet committed-Punkt und Partial-Text über mehrere Fenster.

    RC-A1-Fix (LocalAgreement): Ein Textbereich wird erst committet, wenn er
    in ``commit_agreements`` aufeinanderfolgenden Fenstern identisch ist.
    So kann eine fruhe Halluzination des ersten (kontextlosen) Fensters
    nicht unwiderruflich eingefroren werden.
    """

    def __init__(self, eps: float = 1e-3, commit_agreements: int = 2) -> None:
        self.committed_s = 0.0
        self.committed_text = ""
        self.committed_tokens: list[Token] = []
        self.last_partial = ""
        self._eps = eps
        self.commit_agreements = commit_agreements
        # Kandidaten-Cache: (committed_s, text) des letzten Fensters.
        self._candidate_key: tuple = (0.0, "")
        self._agreement_count = 0

    def skip_silence(self, end_s: float) -> None:
        """Ueberspringt eine stille Passage (Pause) ohne Text zu committen.

        Ohne das bleibt der Commit-Punkt an jeder Sprechpause >= chunk_s
        permanent haengen (das Fenster ist auf committed_s+3s gedeckelt und
        der VAD liefert dauerhaft 'keine Sprache') -> alles nach der Pause
        geht verloren.

        Wichtig: ``end_s`` ist das Ende der STILLEN Passage. Der commit-Punkt
        wird genau dorthin gesetzt (nicht darueber hinaus), damit die danach
        folgenden Sprach-Tokens (start >= end_s) wieder als ``fresh`` gelten.
        """
        if end_s <= self.committed_s:
            return
        self.committed_s = end_s
        self._candidate_key = (round(self.committed_s, 3), "")
        self._agreement_count = 0
        self.last_partial = ""

    def update(
        self, tr: Transcription, commit_horizon: float
    ) -> tuple[str, str]:
        """Verarbeitet eine Fenster-Transkription.

        Returns:
            (new_commit_text, partial_text)
        """
        tokens = tr.tokens
        if not tokens:
            return "", ""

        # Dedupe: nur Tokens nehmen, die zeitlich nach committed_s liegen.
        fresh = [t for t in tokens if t.start >= self.committed_s - self._eps]
        if not fresh:
            return "", self.last_partial

        # Stable = Tokens, die vollständig vor dem Horizont liegen.
        stable = [t for t in fresh if t.end <= commit_horizon + self._eps]

        # Nur bis zur letzten Wortgrenze committen (kein halbes Wort).
        commit_tokens, boundary = _cut_to_word_boundary(stable)
        # Tokens nach dem Horizont + das zurueckgehaltene letzte Wort.
        partial_tokens = [t for t in fresh if t.end > commit_horizon + self._eps]
        # Das zurueckgehaltene letzte Wort (ab boundary) gehoert zum Partial.
        if boundary < len(stable):
            partial_tokens = stable[boundary:] + partial_tokens

        new_commit_text = _join(commit_tokens)
        partial_text = _join(partial_tokens)

        # RC-A1: LocalAgreement gilt nur fuer den ERSTEN Commit (der kommt aus
        # dem kontextlosen Anfangsfenster und kann halluzinieren). Ab dem
        # ersten bestaetigten Commit laeuft das Streaming normal durch -
        # sonst entsteht ein Deadlock, wenn committed_s bei jedem Commit
        # vorrueckt und der Agreement-Cache dadurch nie 2 Treffer sammelt.
        candidate_text = new_commit_text
        already_started = bool(self.committed_text.strip())

        if already_started:
            # Ab dem 2. Commit: direkt committen (kein Agreement mehr noetig).
            if new_commit_text:
                self.committed_tokens.extend(commit_tokens)
                self.committed_text += new_commit_text
                if commit_tokens:
                    self.committed_s = commit_tokens[-1].end
        else:
            # Erster Commit: erst nach ``commit_agreements`` Bestaetigungen.
            candidate_key = (round(self.committed_s, 3), candidate_text)
            if candidate_text and candidate_key == self._candidate_key:
                self._agreement_count += 1
            else:
                self._candidate_key = candidate_key
                self._agreement_count = 1

            if new_commit_text and self._agreement_count >= self.commit_agreements:
                self.committed_tokens.extend(commit_tokens)
                self.committed_text += new_commit_text
                if commit_tokens:
                    self.committed_s = commit_tokens[-1].end
            else:
                new_commit_text = ""  # noch nicht bestaetigt -> nicht committen

        self.last_partial = partial_text
        return new_commit_text, partial_text


def _cut_to_word_boundary(tokens: list[Token]) -> tuple[list[Token], int]:
    """Committet alle Woerter, die vollstaendig vor dem Horizont liegen.

    Ein Wort ist vollstaendig, wenn ALLE seine Subword-Tokens innerhalf der
    ``stable``-Liste liegen (d.h. ihre ``end`` <= Horizont). Da ``stable``
    bereits nur Tokens mit ``end`` <= Horizont enthaelt, ist das hier immer
    der Fall - AUF dem letzten Wort.

    Das letzte Wort ist nur dann sicher vollstaendig, wenn es NICHT am
    audioseitigen Rand abgeschnitten ist. Da wir das innerhalb der Committer-
    Logik nicht wissen (benoetigt den Gesamt-Audio-Kontext), ist die sichere
    Default-Regel fuer das laufende Streaming: das letzte Wort NICHT committen.

    ABER: Diese Funktion committet das letzte Wort DOCH, wenn es eindeutig
    abgeschlossen ist - das erkennen wir daran, dass das letzte Token KEIN
    Wortanfang ist (also das Wort aus einem Wortanfang + Folge-Subwords
    besteht und sein letztes Subword vor dem Horizont endet).

    Fazit: Ein einzelnes Wortanfang-Token am Ende (z.B. " de") ist
    unvollstaendig -> nicht committen. Ein Wort mit Folge-Subword am Ende
    (z.B. " de"+"utlich") ist wahrscheinlich vollstaendig -> committen.
    """
    if not tokens:
        return [], 0
    # Wenn das letzte Token ein Wortanfang ist, ist dieses letzte Wort
    # moeglicherweise unvollstaendig -> zurueckhalten. Alles davor (also
    # alle VORherigen Woerter) ist sicher vollstaendig und wird committet.
    if tokens[-1].is_word_start:
        # Das letzte Wort (das letzte Token) zurueckhalten.
        # Der Rest davor ist committierbar.
        return tokens[:-1], len(tokens) - 1
    # Sonst: letztes Token ist ein Folge-Subword -> Wort vermutlich vollstaendig.
    return list(tokens), len(tokens)


def _join(tokens: list[Token]) -> str:
    return "".join(t.text for t in tokens).strip()