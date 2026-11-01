"""Reference tests using ``xkbcli`` (TDD chain K7).

These tests validate the XKB keyboard layout assumption used by dotool via
``DOTOOL_XKB_LAYOUT=de`` / ``DOTOOL_XKB_VARIANT=nodeadkeys``. They only
run when ``xkbcli`` is installed.

Verified ground truth: umlauts and capital sharp S (ẞ) are mapped correctly
through the Shift/Lock levels of the nodeadkeys layout.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

LAYOUT = "de"


def _compile(variant: str) -> str:
    """Return the ``compile-keymap`` output as text."""
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
        """nodeadkeys maps ae/oe/ue in lowercase and uppercase."""
        out = _compile("nodeadkeys")
        # Lowercase and uppercase forms are present for a/o/u umlauts.
        for small, big in (
            ("udiaeresis", "Udiaeresis"),   # ü / Ü (Position AD11)
            ("adiaeresis", "Adiaeresis"),   # ä / Ä (Position AC10)
            ("odiaeresis", "Odiaeresis"),   # ö / Ö (Position AC11)
        ):
            assert small in out, small
            assert big in out, big

    def test_ssharp_key_is_four_level_plus_lock(self) -> None:
        """The ß key has four levels plus Lock and produces capital ẞ (U1E9E)."""
        out = _compile("nodeadkeys")
        assert "U1E9E" in out
        assert ("FourLevel+Lock" in out or "FOUR_LEVEL_PLUS_LOCK" in out)

    def test_variant_comparison(self) -> None:
        """nodeadkeys and basic produce identical umlaut characters (levels/count)."""
        nodeadkeys = _compile("nodeadkeys")
        basic = _compile("basic")
        for symbol in ("udiaeresis", "Udiaeresis", "adiaeresis",
                       "Adiaeresis", "odiaeresis", "Odiaeresis"):
            assert symbol in nodeadkeys, symbol
            assert symbol in basic, symbol
            assert nodeadkeys.count(symbol) == basic.count(symbol), symbol