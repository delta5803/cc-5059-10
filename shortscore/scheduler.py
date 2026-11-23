"""乐句调度：把 planner 选出的真实段落编排为四个声部的绝对拍事件。

每个段落都由具体材料乐句（A/B 主题及其变体）或终止式构成，
不存在"同一段循环拉满时间"的占位段；收束段最后两小节固定 I-V-I 终止式，
保证最后一个乐句具备由和声材料支持的结束，而不是被任意裁尾。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .harmony import bass_note, pad_voicing, melody_target
from .model import BEATS_PER_BAR, Material, Note, SectionChoice

VOICES = ("melody", "bass", "pad", "perc")


@dataclass
class VoiceEvent:
    voice: str
    pitch: int            # 打击乐使用 pitch=0 的键位约定
    start_beat: float
    dur_beats: float
    vel: float
    section: str
    accent: bool = False


@dataclass
class ScheduledScore:
    events: list[VoiceEvent] = field(default_factory=list)
    structural: list[dict] = field(default_factory=list)   # 结构事件（高潮/回归等）

    def add(self, voice, pitch, start, dur, vel, section, accent=False):
        self.events.append(VoiceEvent(voice, pitch, start, dur, vel, section, accent))


# ---------------------------------------------------------------------------

def _shift_notes(notes: list[Note], offset_beat: float, semitones: int,
                 vel_scale: float, section: str, voice: str,
                 out: ScheduledScore, limit_beat: float | None = None,
                 accent_beats: set[float] | None = None):
    for n in notes:
        start = offset_beat + n.start
        if limit_beat is not None and start >= limit_beat - 1e-9:
            continue
        dur = n.dur
        if limit_beat is not None:
            dur = min(dur, limit_beat - start)
        if dur <= 0:
            continue
        pitch = n.pitch + semitones if n.pitch is not None else None
        if pitch is None:
            continue
        out.add(voice, pitch, start, dur, max(0.05, n.vel * vel_scale), section,
                accent=bool(accent_beats and abs(n.start) < 1e-9 and start in accent_beats))


def _emit_phrase(mat: Material, start_beat: float, bars: int, transform: str,
                 section: str, out: ScheduledScore, limit_beat: float,
                 climax_downbeat: float | None):
    """在 start_beat 放置一个材料乐句（按 transform 变奏），长度 bars 小节。"""
    pb = BEATS_PER_BAR * bars
    end = min(start_beat + pb, limit_beat)
    semitone = {"repeat": 0, "octave_up": 12, "sequence_up": 2,
                "sequence_down": -2, "soft": 0}[transform]
    vel_mel = 0.6 if transform == "soft" else 1.0
    vel_bass = 0.75 if transform == "soft" else 1.0
    accents = {climax_downbeat} if climax_downbeat is not None else set()
    _shift_notes(mat.melody, start_beat, semitone, vel_mel, section, "melody", out, end, accents)
    _shift_notes(mat.bass, start_beat, 0, vel_bass, section, "bass", out, end)
    # 和声垫：按材料和弦标签，每和弦持续到下一个和弦
    chord_beats = sorted(set(c[0] for c in mat.chords)) + [mat.phrase_bars * BEATS_PER_BAR]
    for i, (cb, label) in enumerate(mat.chords):
        cstart = start_beat + cb
        if cstart >= end:
            break
        cend = min(start_beat + chord_beats[i + 1], end)
        for p in pad_voicing(label, 60):
            out.add("pad", p, cstart, cend - cstart, 0.30 if transform != "soft" else 0.22,
                    section)
    if transform != "soft" and mat.percussion:
        _shift_notes(mat.percussion, start_beat, 0, 0.9, section, "perc", out, end)
    if climax_downbeat is not None and end > climax_downbeat >= start_beat:
        # 高潮落点：四个声部同时重音（定音鼓式单击 + 全声部强调）
        out.add("perc", 1, climax_downbeat, 1.0, 1.0, section, accent=True)
        for p in pad_voicing("C", 60):
            out.add("pad", p, climax_downbeat, 2.0, 0.55, section, accent=True)


def _emit_cadence(out: ScheduledScore, start_beat: float, section: str,
                  tonic_label: str, dominant_label: str):
    """最后两小节的 I-V-I 正格终止（材料驱动的结束，禁止裁尾对齐）。"""
    b = BEATS_PER_BAR
    # V 和弦一小节
    v_start = start_beat
    out.add("bass", bass_note(dominant_label, 48), v_start, b, 0.8, section)
    for p in pad_voicing(dominant_label, 60):
        out.add("pad", p, v_start, b, 0.4, section)
    out.add("melody", melody_target(dominant_label, 74), v_start + b - 1, 1.0, 0.85, section)
    out.add("perc", 0, v_start, 1, 0.5, section)
    # I 和弦一小节，主音全音符收束
    i_start = start_beat + b
    out.add("bass", bass_note(tonic_label, 48), i_start, b, 0.9, section)
    for p in pad_voicing(tonic_label, 60):
        out.add("pad", p, i_start, b, 0.45, section)
    out.add("melody", melody_target(tonic_label, 72), i_start, b, 0.95, section, accent=True)
    out.add("perc", 1, i_start, b, 0.9, section, accent=True)


# ---------------------------------------------------------------------------

def _development_plan(bars: int, phrase_bars: int) -> list[tuple[str, str, int]]:
    """返回 (主题, 变奏, 乐句长度小节)。

    发展段至少包含两个对比乐句；偶数小节整乐句排列，末尾零头用半乐句消化，
    保证高潮永远落在发展段最后一个乐句的强拍而不是段首。
    """
    full = bars // phrase_bars
    rem = bars - full * phrase_bars
    plan: list[tuple[str, str, int]] = []
    if full >= 2:
        plan.append(("B", "repeat", phrase_bars))
        plan.append(("B", "sequence_up", phrase_bars))
        for i in range(2, full):
            plan.append(("A" if i % 2 else "B",
                         "sequence_down" if i % 2 else "octave_up", phrase_bars))
    else:
        # 短发展段：两个半乐句（B 起、A 应答），仍构成对比与推进
        half = max(1, phrase_bars // 2)
        plan.append(("B", "repeat", half))
        plan.append(("A", "sequence_up", bars - half))
        return plan
    if rem > 0:
        plan.append(("A", "sequence_down", rem))
    return plan


def build_schedule(sections: list[SectionChoice], materials: dict[str, Material],
                   phrase_bars: int) -> ScheduledScore:
    out = ScheduledScore()
    climax_beat = None
    theme_return_beat = None

    for sec in sections:
        start = sec.start_beat
        limit = start + sec.bars * BEATS_PER_BAR
        out.structural.append({"kind": "section_start", "section": sec.kind,
                               "label": sec.variant, "beat": start})

        if sec.kind == "intro":
            n = max(1, sec.bars // phrase_bars)
            for i in range(n):
                _emit_phrase(materials["A"], start + i * phrase_bars * BEATS_PER_BAR,
                             phrase_bars, "soft", sec.kind, out, limit, None)
            rem = sec.bars - n * phrase_bars
            if rem > 0:
                _emit_phrase(materials["A"], start + n * phrase_bars * BEATS_PER_BAR,
                             rem, "soft", sec.kind, out, limit, None)

        elif sec.kind == "development":
            plan = _development_plan(sec.bars, phrase_bars)
            sec.variant = "→".join(f"{t}:{v}" for t, v, _ in plan)
            cursor = start
            last_start = start + sum(pb * BEATS_PER_BAR for _, _, pb in plan[:-1])
            for i, (theme, transform, pb) in enumerate(plan):
                cdb = last_start if i == len(plan) - 1 else None
                _emit_phrase(materials[theme], cursor, pb, transform,
                             sec.kind, out, limit, cdb)
                if cdb is not None:
                    climax_beat = cdb
                cursor += pb * BEATS_PER_BAR

        elif sec.kind == "recap":
            n = max(1, sec.bars // phrase_bars)
            theme_return_beat = start
            for i in range(n):
                transform = "octave_up" if i == 0 else ("repeat" if i % 2 else "sequence_up")
                _emit_phrase(materials["A"], start + i * phrase_bars * BEATS_PER_BAR,
                             phrase_bars, transform, sec.kind, out, limit, None)
            rem = sec.bars - n * phrase_bars
            if rem > 0:
                _emit_phrase(materials["A"], start + n * phrase_bars * BEATS_PER_BAR,
                             rem, "repeat", sec.kind, out, limit, None)

        elif sec.kind == "coda":
            cad_bars = 2
            body_bars = sec.bars - cad_bars
            cursor = start
            if body_bars > 0:
                n = body_bars // phrase_bars
                for i in range(n):
                    _emit_phrase(materials["A"], cursor, phrase_bars, "soft",
                                 sec.kind, out, start + body_bars * BEATS_PER_BAR, None)
                    cursor += phrase_bars * BEATS_PER_BAR
                rem = body_bars - n * phrase_bars
                if rem > 0:
                    _emit_phrase(materials["A"], cursor, rem, "soft", sec.kind, out,
                                 start + body_bars * BEATS_PER_BAR, None)
                    cursor += rem * BEATS_PER_BAR
            _emit_cadence(out, cursor, sec.kind, "C", "G")

    if climax_beat is not None:
        out.structural.append({"kind": "climax", "section": "development",
                               "beat": climax_beat, "label": "高潮落点"})
    if theme_return_beat is not None:
        out.structural.append({"kind": "theme_return", "section": "recap",
                               "beat": theme_return_beat, "label": "主题回归"})
    return out
