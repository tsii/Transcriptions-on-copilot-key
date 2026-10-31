"""Bracketed-Paste-Marker-Filter (Betreiber 31.10.):

Manuelles Paste mit aktiverem Bracketed-Paste (Shell wartet nach
vorherigem Strg+V im quoted-insert) bringt ESC[200~...ESC[201~
in den getippten/prasentierten Text — sichtbar als '^[[200~' und
'~' am Ende. Beide Darstellungen (roh und ^notiert) muessen aus
jedem Text rausgefiltert werden, BEVOR die Delta-Berechnung laeuft.
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
