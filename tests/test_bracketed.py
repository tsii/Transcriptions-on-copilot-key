"""Bracketed-paste marker filter (operator, October 31):

Manual paste with bracketed paste enabled (the shell waits after a previous
Ctrl+V in quoted-insert) inserts ESC[200~...ESC[201~ into the typed/presented
text — visible as '^[[200~' and '~' at the end. Both representations (raw and
caret notation) must be filtered out of every text BEFORE delta calculation.
"""
from __future__ import annotations

from stt_button.typing import strip_paste_markers


class TestStripPasteMarkers:
    def test_raw_markers_stripped(self) -> None:
        s = "\x1b[200~Hallo Welt\x1b[201~"
        assert strip_paste_markers(s) == "Hallo Welt"

    def test_caret_notation_stripped(self) -> None:
        s = "^[[200~Hallo Welt~"
        assert strip_paste_markers(s) == "Hallo Welt"

    def test_mixed_both(self) -> None:
        s = "^[[200~Teil\x1b[200~weiter~"
        assert strip_paste_markers(s) == "Teilweiter"

    def test_plain_text_untouched(self) -> None:
        s = "Hallo Welt, wie geht's? 123,45"
        assert strip_paste_markers(s) == s

    def test_empty(self) -> None:
        assert strip_paste_markers("") == ""

    def test_only_markers(self) -> None:
        assert strip_paste_markers("^[[200~~") == ""
