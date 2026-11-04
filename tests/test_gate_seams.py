"""Unit tests for the seam analysis of tools/gate_eval.py (Phase 2, C4)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "tools" / "gate_eval.py"
_spec = importlib.util.spec_from_file_location("gate_eval", _PATH)
gate_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate_eval)

REF = "Das ist der erste Satz. Und hier kommt der zweite Satz."


def test_identical_is_ok() -> None:
    assert gate_eval.seam_defects(REF, REF, [5]) == ["ok"]


def test_lost_word_at_seam() -> None:
    cand = "Das ist der erste Satz. hier kommt der zweite Satz."
    assert gate_eval.seam_defects(REF, cand, [5]) == ["lost"]


def test_doubled_word_at_seam() -> None:
    cand = "Das ist der erste Satz. Satz. Und hier kommt der zweite Satz."
    assert gate_eval.seam_defects(REF, cand, [5]) == ["doubled"]


def test_changed_word_at_seam() -> None:
    cand = "Das ist der erste Satz. Und dort kommt der zweite Satz."
    assert gate_eval.seam_defects(REF, cand, [5]) == ["changed"]


def test_capitalization_and_punctuation_ignored() -> None:
    cand = "Das ist der erste Satz und hier kommt der zweite Satz."
    assert gate_eval.seam_defects(REF, cand, [5]) == ["ok"]


def test_defect_far_from_seam_ignored() -> None:
    cand = "Das ist der erste Satz. Und hier kommt der zweite."
    assert gate_eval.seam_defects(REF, cand, [5]) == ["ok"]


def test_seam_indices_from_closed_texts() -> None:
    assert gate_eval.seam_word_indices(["Eins zwei. ", "Eins zwei. Drei. "]) == [2, 3]


def test_cap_after_seam() -> None:
    cand = "Eins zwei. drei vier."
    assert gate_eval.cap_after_seam(cand, [2]) == [False]
