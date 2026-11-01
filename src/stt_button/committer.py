"""TimestampCommitter: uses token timestamps to determine what is
committed permanently (never revised) and what remains partial (correctable).

Core idea from the Kimi research: Only tokens whose ``end`` lies entirely
before the commit horizon AND that extend through the last word boundary are
committed. Window overlaps are deduplicated using timestamps.
"""
from __future__ import annotations

from .streaming_types import Token, Transcription

FRAME_S = 0.08  # TDT: hop 160 @16kHz * subsampling 8


class TimestampCommitter:
    """Manages the commit point and partial text across multiple windows.

    RC-A1 fix (LocalAgreement): A text region is committed only after it is
    identical across ``commit_agreements`` consecutive windows.
    This prevents an early hallucination in the first (context-free) window
    from being permanently frozen into the output.
    """

    def __init__(
        self,
        eps: float = 1e-3,
        commit_agreements: int = 2,
        require_agreement_always: bool = False,
    ) -> None:
        self.committed_s = 0.0
        self.committed_text = ""
        self.committed_tokens: list[Token] = []
        self.last_partial = ""
        self._eps = eps
        self.commit_agreements = commit_agreements
        #: STAGE 3: True => EVERY candidate requires ``commit_agreements``
        #: consecutive identical occurrences at the same committed_s (no
        #: direct commit after the second commit). Default False preserves
        #: the previous windowed behavior.
        self.require_agreement_always = require_agreement_always
        # Candidate cache: (committed_s, text) from the last window.
        self._candidate_key: tuple = (0.0, "")
        self._agreement_count = 0

    def skip_silence(self, end_s: float) -> None:
        """Skips a silent passage (pause) without committing text.

        Without this, the commit point stalls permanently at every speech
        pause >= chunk_s (the window is capped at committed_s+3s and VAD
        continuously reports 'no speech') -> everything after the pause is
        lost.

        Important: ``end_s`` is the end of the SILENT passage. The commit
        point is set exactly there (not beyond it), so subsequent speech
        tokens (start >= end_s) are once again considered ``fresh``.
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
        """Processes one window transcription.

        Returns:
            (new_commit_text, partial_text)
        """
        tokens = tr.tokens
        if not tokens:
            return "", ""

        # Dedupe: use only tokens whose timestamps follow committed_s.
        fresh = [t for t in tokens if t.start >= self.committed_s - self._eps]
        if not fresh:
            return "", self.last_partial

        # Stable = tokens that lie entirely before the horizon.
        stable = [t for t in fresh if t.end <= commit_horizon + self._eps]

        # Commit only through the last word boundary (no partial words).
        commit_tokens, boundary = _cut_to_word_boundary(stable)
        # Tokens beyond the horizon plus the withheld last word.
        partial_tokens = [t for t in fresh if t.end > commit_horizon + self._eps]
        # The withheld last word (from boundary onward) belongs to the partial.
        if boundary < len(stable):
            partial_tokens = stable[boundary:] + partial_tokens

        new_commit_text = _join(commit_tokens)
        partial_text = _join(partial_tokens)

        # RC-A1: LocalAgreement applies only to the FIRST commit (which comes
        # from the context-free initial window and can hallucinate). After the
        # first confirmed commit, streaming proceeds normally; otherwise, a
        # deadlock occurs as committed_s advances on every commit and the
        # agreement cache never collects 2 matches.
        candidate_text = new_commit_text
        already_started = bool(self.committed_text.strip())

        if already_started and not self.require_agreement_always:
            # From the 2nd commit onward: commit directly (no agreement needed).
            if new_commit_text:
                self.committed_tokens.extend(commit_tokens)
                self.committed_text += new_commit_text
                if commit_tokens:
                    self.committed_s = commit_tokens[-1].end
        else:
            # First commit: wait for ``commit_agreements`` confirmations.
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
                new_commit_text = ""  # not confirmed yet -> do not commit

        self.last_partial = partial_text
        return new_commit_text, partial_text


def _cut_to_word_boundary(tokens: list[Token]) -> tuple[list[Token], int]:
    """Commits all words that lie entirely before the horizon.

    A word is complete when ALL of its subword tokens are in the ``stable``
    list (i.e., their ``end`` <= horizon). Since ``stable`` already contains
    only tokens with ``end`` <= horizon, this is always the case here EXCEPT
    for the last word.

    The last word is reliably complete only if it has NOT been cut off at the
    audio edge. The committer cannot determine this internally (it requires
    the full audio context), so the safe default for ongoing streaming is:
    do NOT commit the last word.

    HOWEVER: This function DOES commit the last word when it is unambiguously
    complete. We infer this when the last token is NOT a word start (meaning
    the word consists of a word start followed by subwords, with its final
    subword ending before the horizon).

    Conclusion: A single word-start token at the end (e.g., " de") is
    incomplete -> do not commit. A word ending with a subsequent subword
    (e.g., " de"+"utlich") is probably complete -> commit.
    """
    if not tokens:
        return [], 0
    # If the last token is a word start, this final word may be incomplete
    # -> withhold it. Everything before it (all PREVIOUS words) is reliably
    # complete and is committed.
    if tokens[-1].is_word_start:
        # Withhold the last word (the last token).
        # Everything before it can be committed.
        return tokens[:-1], len(tokens) - 1
    # Otherwise: last token is a subsequent subword -> word probably complete.
    return list(tokens), len(tokens)


def _join(tokens: list[Token]) -> str:
    return "".join(t.text for t in tokens).strip()