"""Referenz-Tests ueber ``xkbcli`` (TDD-Kette K7).

Diese Tests validieren die XKB-Tastaturlayout-Annahme, die dotool ueber
``DOTOOL_XKB_LAYOUT=de`` / ``DOTOOL_XKB_VARIANT=nodeadkeys`` verwendet. Sie
laufen nur, wenn ``xkbcli`` installiert ist.

Ground Truth (belegt): Umlaute und das grosse ß (ẞ) werden ueber die
Shift-/Lock-Ebenen des nodeadkeys-Layouts korrekt gemappt.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

LAYOUT = "de"


def _compile(variant: str) -> str:
    """Gibt die ``compile-keymap``-Ausgabe als Text zurueck."""
    proc = subprocess.run(
        ["xkbcli", "compile-keymap", "--layout", LAYOUT, "--variant", variant],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.mark.skipif(shutil.which("xkbcli") is None, reason="xkbcli fehlt")
class TestKeymapReference:
    def test_nodeadkeys_umlaut_levels(self) -> None:
        """nodeadkeys bildet ae/oe/ue in Klein- und Grossschreibung ab."""
        out = _compile("nodeadkeys")
        # Klein-/Grossschreibung fuer a/o/u-Umlaut vorhanden.
        for small, big in (
            ("udiaeresis", "Udiaeresis"),   # ü / Ü (Position AD11)
            ("adiaeresis", "Adiaeresis"),   # ä / Ä (Position AC10)
            ("odiaeresis", "Odiaeresis"),   # ö / Ö (Position AC11)
        ):
            assert small in out, small
            assert big in out, big

    def test_ssharp_key_is_four_level_plus_lock(self) -> None:
        """Die ß-Taste hat vier Level plus Lock und liefert das grosse ẞ (U1E9E)."""
        out = _compile("nodeadkeys")
        assert "U1E9E" in out
        assert ("FourLevel+Lock" in out or "FOUR_LEVEL_PLUS_LOCK" in out)

    def test_variant_comparison(self) -> None:
        """nodeadkeys und basic liefern identische Umlaut-Zeichen (Level/Anzahl)."""
        nodeadkeys = _compile("nodeadkeys")
        basic = _compile("basic")
        for symbol in ("udiaeresis", "Udiaeresis", "adiaeresis",
                       "Adiaeresis", "odiaeresis", "Odiaeresis"):
            assert symbol in nodeadkeys, symbol
            assert symbol in basic, symbol
            assert nodeadkeys.count(symbol) == basic.count(symbol), symbol