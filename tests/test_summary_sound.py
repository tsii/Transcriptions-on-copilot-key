"""Tests for play_error_sound in stt_button.summary."""

from __future__ import annotations

from pathlib import Path

from stt_button.summary import play_error_sound


class FakePopen:
    """Records instantiations instead of playing a real sound."""

    calls: list[list] = []
    instances: list["FakePopen"] = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        # Popen is called as popen(argv_list, ...): record the argv list flat,
        # so calls[i][-1] is the asset path and "--volume" is findable in it.
        FakePopen.calls.append(list(args[0]) if args else [])
        FakePopen.instances.append(self)

    @classmethod
    def reset(cls):
        cls.calls = []
        cls.instances = []


def test_play_error_sound_calls_paplay_with_absolute_path():
    FakePopen.reset()
    result = play_error_sound("you_lose", 24000, popen=FakePopen)
    assert result is True
    assert len(FakePopen.calls) == 1
    args = FakePopen.calls[0]
    path = Path(args[-1])
    assert path.is_absolute()
    assert path.name == "you_lose.wav"
    assert "--volume" in args
    assert "24000" in args


def test_play_error_sound_swallows_exception():
    class ExplodingPopen:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("boom")

    assert play_error_sound("you_lose", 24000, popen=ExplodingPopen) is False


def test_error_sound_asset_exists():
    asset = Path(__file__).resolve().parent.parent / "src" / "stt_button" / "sounds" / "you_lose.wav"
    assert asset.exists()
    assert asset.stat().st_size > 0
