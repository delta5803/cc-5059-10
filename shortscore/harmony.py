"""和弦标签解析与音区排布（仅依赖内置规则，无外部库）。"""
from __future__ import annotations

import re

_CHORD_INTERVALS = {
    "": (0, 4, 7),
    "maj": (0, 4, 7),
    "m": (0, 3, 7),
    "min": (0, 3, 7),
    "7": (0, 4, 7, 10),
    "maj7": (0, 4, 7, 11),
    "m7": (0, 3, 7, 10),
    "min7": (0, 3, 7, 10),
    "dim": (0, 3, 6),
    "dim7": (0, 3, 6, 9),
}
_ROOT_SEMITONE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def parse_chord(label: str) -> tuple[int, tuple[int, ...]]:
    """返回 (根音音级 0..11, 和弦音程)。"""
    m = re.match(r"^([A-Ga-g])([#b]?)(.*)$", label.strip())
    if not m:
        raise ValueError(f"无法解析和弦标签: {label}")
    letter, acc, quality = m.groups()
    root = _ROOT_SEMITONE[letter.upper()] + (1 if acc == "#" else -1 if acc == "b" else 0)
    root %= 12
    return root, _CHORD_INTERVALS.get(quality, _CHORD_INTERVALS[""])


def _nearest(pc: int, around: int) -> int:
    """把音级 pc 放到 around 附近的 MIDI 音高。"""
    return around + ((pc - around) % 12)


def bass_note(label: str, octave_midi: int = 48) -> int:
    root, _ = parse_chord(label)
    return _nearest(root, octave_midi)


def pad_voicing(label: str, around: int = 60) -> list[int]:
    root, intervals = parse_chord(label)
    return [_nearest((root + iv) % 12, around) for iv in intervals]


def melody_target(label: str, around: int = 72) -> int:
    root, intervals = parse_chord(label)
    # 取和弦三音/根音中最靠近旋律音区的音
    pcs = [(root + iv) % 12 for iv in intervals]
    return min((_nearest(pc, around) for pc in pcs), key=lambda x: abs(x - around))
