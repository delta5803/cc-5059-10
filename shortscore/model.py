"""数据模型与规格（spec）加载。

输入 JSON 的字段说明见 docs/USAGE.md。所有时长内部以秒计，节拍以拍计（4/4：1 小节 = 4 拍）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

BEATS_PER_BAR = 4

# 受支持的固定段落顺序（引子/发展/回归/收束）
CANONICAL_ORDER = ["intro", "development", "recap", "coda"]
SECTION_CN = {
    "intro": "引子",
    "development": "发展",
    "recap": "回归",
    "coda": "收束",
}
# 各段默认可选小节数范围（planner 在此网格上枚举真实段落）
DEFAULT_BAR_RANGES = {
    "intro": (0, 1, 2, 4),          # 0 表示可省略
    "development": (4, 6, 8),
    "recap": (4, 6, 8),
    "coda": (0, 2, 3, 4),           # 0 表示可省略
}
DEFAULT_TEMPO_BPM = {
    "intro": 72,
    "development": 104,
    "recap": 96,
    "coda": 80,
}


@dataclass
class Note:
    """单个音符。pitch 为 MIDI 音高（60 = 中央C），None 表示休止；dur 以拍计。"""
    pitch: int | None
    start: float          # 相对乐句/小节起点的拍位置
    dur: float
    vel: float = 0.8      # 力度 0~1


@dataclass
class Material:
    """用户自制主题材料：旋律、低音、和弦标签与打击乐模式。"""
    name: str
    melody: list[Note]
    bass: list[Note]
    chords: list[tuple[float, str]]      # (起拍, 和弦标签如 "C"、"Am"、"G7")
    percussion: list[Note] = field(default_factory=list)
    phrase_bars: int = 4


@dataclass
class Emphasis:
    """强调时点：role 决定它绑定的真实音乐事件。"""
    time: float
    role: str            # section_start:<段名> / climax / theme_return
    tolerance: float = 0.5
    label: str = ""


@dataclass
class SectionChoice:
    """planner 选出的一个真实段落（不是循环占位）。"""
    kind: str
    bars: int
    start_beat: float
    start_sec: float
    bpm_start: float
    bpm_end: float
    variant: str = ""                 # 发展段乐句计划等
    statements: dict[str, int] = field(default_factory=dict)  # 本段各主题陈述次数


@dataclass
class Plan:
    sections: list[SectionChoice]
    total_bars: int
    total_beats: float
    bpm_scale: float
    target_sec: float
    delivery_sec: float
    nominal_sec: float                # 到最后一拍起点的时长（不含尾音释放）
    emphasis_events: list[dict[str, Any]]
    phrase_bars: int
    phrase_divides: bool              # 乐句长度（小节）是否整除总片长（小节）
    theme_counts: dict[str, int]
    tail_policy: str
    score: dict[str, Any]
    feasible: bool
    violations: list[str]
    schedule: list[dict[str, Any]] = field(default_factory=list)
    tempo_curve: list[dict[str, float]] = field(default_factory=list)
    comparison: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 内置默认主题（用户未给材料时使用；用户材料优先，见 load_spec）
# ---------------------------------------------------------------------------

def _notes(pairs: list[tuple[int | None, float, float]], base_beat: float = 0.0) -> list[Note]:
    out, t = [], base_beat
    for pitch, dur, vel in pairs:
        if pitch is not None:
            out.append(Note(pitch, t, dur, vel))
        t += dur
    return out


def default_materials() -> dict[str, Material]:
    # 主题 A：C 大调，4 小节，陈述/回归使用
    a_mel = _notes([
        (72, 1, .8), (76, 1, .8), (79, 1, .85), (76, 1, .75),
        (74, 1, .8), (77, 1, .8), (81, 1, .85), (77, 1, .75),
        (76, 1, .8), (74, 1, .8), (72, 1, .85), (74, 1, .75),
        (72, 2, .9), (76, 2, .9),
    ])
    a_bass = _notes([
        (48, 2, .7), (55, 2, .7),
        (45, 2, .7), (52, 2, .7),
        (53, 2, .7), (55, 2, .7),
        (48, 4, .8),
    ])
    a_chords = [(0, "C"), (4, "G"), (8, "F"), (12, "C")]
    # 主题 B：a 小调对比材料
    b_mel = _notes([
        (69, 1, .8), (72, 1, .85), (76, 1, .85), (72, 1, .75),
        (71, 1, .8), (74, 1, .85), (77, 1, .9), (74, 1, .8),
        (72, 2, .85), (69, 2, .8),
        (67, 2, .8), (69, 2, .85),
    ])
    b_bass = _notes([
        (45, 2, .7), (45, 2, .7),
        (52, 2, .7), (50, 2, .7),
        (43, 2, .7), (48, 2, .7),
        (45, 4, .8),
    ])
    b_chords = [(0, "Am"), (4, "Em"), (8, "G"), (12, "Am")]
    perc = [Note(0, 0, 1, .5), Note(0, 2, 1, .5),
            Note(0, 4, 1, .5), Note(0, 6, 1, .5),
            Note(0, 8, 1, .5), Note(0, 10, 1, .5),
            Note(0, 12, 1, .6), Note(0, 14, 1, .6)]
    return {
        "A": Material("主题A", a_mel, a_bass, a_chords, perc, phrase_bars=4),
        "B": Material("主题B", b_mel, b_bass, b_chords, perc, phrase_bars=4),
    }


# ---------------------------------------------------------------------------
# spec 解析
# ---------------------------------------------------------------------------

def _parse_note_list(raw: list[dict[str, Any]]) -> list[Note]:
    notes = []
    for n in raw or []:
        notes.append(Note(
            pitch=n.get("pitch"),
            start=float(n["start"]),
            dur=float(n["dur"]),
            vel=float(n.get("vel", 0.8)),
        ))
    notes.sort(key=lambda x: x.start)
    return notes


def _parse_material(raw: dict[str, Any], key: str) -> Material:
    return Material(
        name=raw.get("name", key),
        melody=_parse_note_list(raw.get("melody", [])),
        bass=_parse_note_list(raw.get("bass", [])),
        chords=[(float(c[0]), str(c[1])) for c in raw.get("chords", [])],
        percussion=_parse_note_list(raw.get("percussion", [])),
        phrase_bars=int(raw.get("phrase_bars", 4)),
    )


def load_spec(raw: dict[str, Any]) -> dict[str, Any]:
    """把原始 dict 规范化为内部 spec；缺失字段用默认值补齐，用户材料优先。"""
    materials = default_materials()
    for key, mraw in (raw.get("materials") or {}).items():
        materials[key] = _parse_material(mraw, key)

    emphases = []
    for e in raw.get("emphases", []):
        emphases.append(Emphasis(
            time=float(e["time"]),
            role=str(e["role"]),
            tolerance=float(e.get("tolerance", 0.5)),
            label=str(e.get("label", "")),
        ))

    bar_ranges = {k: tuple(v) for k, v in (raw.get("bar_ranges") or {}).items()}
    for k, v in DEFAULT_BAR_RANGES.items():
        bar_ranges.setdefault(k, v)
    tempo_bpm = dict(DEFAULT_TEMPO_BPM)
    tempo_bpm.update(raw.get("tempo_bpm") or {})

    allowed = raw.get("allowed_sections")
    allowed = list(allowed) if allowed else list(CANONICAL_ORDER)

    recurrence = raw.get("recurrence") or {}
    recurrence.setdefault("A", (2, 3))
    recurrence.setdefault("B", (1, 2))
    recurrence = {k: tuple(v) for k, v in recurrence.items()}

    return {
        "title": str(raw.get("title", "未命名配乐")),
        "materials": materials,
        "target_sec": float(raw.get("target_sec", 60.0)),
        "emphases": emphases,
        "allowed_sections": [s for s in CANONICAL_ORDER if s in allowed],
        "bar_ranges": bar_ranges,
        "tempo_bpm": {k: float(v) for k, v in tempo_bpm.items()},
        "tempo_limits": tuple(raw.get("tempo_limits", (0.75, 1.35))),
        "tail_policy": str(raw.get("tail_policy", "include_release")),
        "release_sec": float(raw.get("release_sec", 0.45)),
        "phrase_bars": int(raw.get("phrase_bars", 4)),
        "recurrence": recurrence,
        "sample_rate": int(raw.get("sample_rate", 44100)),
        "seed": int(raw.get("seed", 7)),
    }
