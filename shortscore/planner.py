"""曲式规划器。

在用户允许的小节网格上枚举真实段落组合（引子/发展/回归/收束），
由目标片长反解整体速度缩放，再把强调时点与真实音乐事件对齐。
两种对齐策略可比较：
  * tempo_map     ：结构固定，改变速度曲线（整体缩放，末段可渐慢）
  * section_replace：速度保持名义值，用替换/增删乐段的小节组合逼近时长
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

from .model import (BEATS_PER_BAR, CANONICAL_ORDER, Plan, SectionChoice,
                    load_spec)
from .scheduler import build_schedule
from .timeline import Segment, TempoMap, ramp_toward, scale_segments


@dataclass
class Candidate:
    bars: dict[str, int]
    total_bars: int
    scale: float
    delivery_sec: float
    nominal_sec: float
    max_emph_err: float
    duration_err: float
    emphasis: list[dict]
    theme_counts: dict[str, int]
    phrase_divides: bool
    violations: list[str]
    score: float
    tempo_map: TempoMap
    segments: list[Segment]


def _segments_for(bars: dict[str, int], tempo_bpm: dict[str, float]) -> list[Segment]:
    segs = []
    for kind in CANONICAL_ORDER:
        if bars.get(kind, 0) > 0:
            bpm = tempo_bpm[kind]
            segs.append(Segment(kind, bars[kind] * BEATS_PER_BAR, bpm, bpm))
    return segs


def _dev_plan_shape(bars: int, phrase_bars: int) -> list[tuple[str, int]]:
    """与 scheduler._development_plan 保持一致的 (主题, 乐句小节数) 形状。"""
    full = bars // phrase_bars
    rem = bars - full * phrase_bars
    shape: list[tuple[str, int]] = []
    if full >= 2:
        shape.append(("B", phrase_bars))
        shape.append(("B", phrase_bars))
        for i in range(2, full):
            shape.append(("A" if i % 2 else "B", phrase_bars))
        if rem > 0:
            shape.append(("A", rem))
    else:
        half = max(1, phrase_bars // 2)
        shape.append(("B", half))
        shape.append(("A", bars - half))
    return shape


def _theme_counts(bars: dict[str, int], phrase_bars: int) -> dict[str, int]:
    intro = bars.get("intro", 0) // phrase_bars
    dev_shape = _dev_plan_shape(bars.get("development", 0), phrase_bars)
    dev_a = sum(1 for t, _ in dev_shape if t == "A")
    dev_b = sum(1 for t, _ in dev_shape if t == "B")
    recap = bars.get("recap", 0) // phrase_bars
    coda = bars.get("coda", 0) // phrase_bars
    return {"A": intro + dev_a + recap + coda, "B": dev_b}


def _event_beats(bars: dict[str, int], phrase_bars: int) -> dict[str, float]:
    """结构事件 -> 绝对拍位置（与 scheduler 的 structural 事件一致）。"""
    events: dict[str, float] = {}
    cursor = 0.0
    for kind in CANONICAL_ORDER:
        n = bars.get(kind, 0)
        if not n:
            continue
        events[f"section_start:{kind}"] = cursor
        if kind == "development":
            shape = _dev_plan_shape(n, phrase_bars)
            climax_offset_bars = sum(pb for _, pb in shape[:-1])
            events["climax"] = cursor + climax_offset_bars * BEATS_PER_BAR
        if kind == "recap":
            events["theme_return"] = cursor
        cursor += n * BEATS_PER_BAR
    return events


def _score_emphasis(spec: dict, event_beats: dict[str, float],
                    tmap: TempoMap) -> tuple[list[dict], list[str], float, float]:
    rows, violations, max_err, norm_err = [], [], 0.0, 0.0
    for e in spec["emphases"]:
        if e.role not in event_beats:
            violations.append(f"强调事件 {e.role or e.label} 对应的段落未被选入，无法落点")
            rows.append({"role": e.role, "label": e.label, "target": e.time,
                         "actual": None, "error": None, "tolerance": e.tolerance,
                         "matched": False})
            norm_err = max(norm_err, 1e9)
            continue
        actual = tmap.time_at_beat(event_beats[e.role])
        err = actual - e.time
        rows.append({"role": e.role, "label": e.label, "target": round(e.time, 4),
                     "actual": round(actual, 4), "error": round(err, 4),
                     "tolerance": e.tolerance,
                     "matched": abs(err) <= e.tolerance + 1e-9})
        max_err = max(max_err, abs(err))
        norm_err = max(norm_err, abs(err) / max(e.tolerance, 1e-6))
        if abs(err) > e.tolerance + 1e-9:
            violations.append(
                f"强调 {e.label or e.role} 落点 {actual:.2f}s 偏离目标 {e.time:.2f}s "
                f"达 {abs(err):.2f}s，超过容差 ±{e.tolerance}s")
    ordered = [r for r in rows if r["actual"] is not None]
    for r1, r2 in zip(ordered, ordered[1:]):
        if r2["actual"] < r1["actual"] - 1e-9:
            violations.append(f"强调顺序冲突：{r1['role']} 晚于 {r2['role']}")
            norm_err = max(norm_err, 1e9)
    return rows, violations, max_err, norm_err


def _evaluate(bars: dict[str, int], spec: dict, strategy: str,
              segments: list[Segment] | None = None,
              preset_scale: float | None = None) -> Candidate | None:
    target = spec["target_sec"]
    base_segs = _segments_for(bars, spec["tempo_bpm"])
    if not base_segs:
        return None
    base_map = TempoMap([Segment(s.name, s.beats, s.bpm_start, s.bpm_end)
                         for s in base_segs])
    nominal_sec = base_map.total_sec
    release = spec["release_sec"] if spec["tail_policy"] == "include_release" else 0.0

    if segments is None:
        if strategy == "tempo_map":
            scale = nominal_sec / max(target - release, 1e-6)
            segs = scale_segments(base_segs, scale)
        else:
            scale = 1.0
            segs = [Segment(s.name, s.beats, s.bpm_start, s.bpm_end) for s in base_segs]
    else:
        # 调用方已按目标交付时长完成精确缩放（如末段渐慢变体）
        scale = preset_scale if preset_scale is not None else \
            nominal_sec / max(TempoMap(segments).total_sec, 1e-6)
        segs = [Segment(s.name, s.beats, s.bpm_start, s.bpm_end) for s in segments]
    tmap = TempoMap(segs)
    delivery_sec = tmap.total_sec + release

    event_beats = _event_beats(bars, spec["phrase_bars"])
    emph_rows, violations, max_err, norm_err = _score_emphasis(spec, event_beats, tmap)

    counts = _theme_counts(bars, spec["phrase_bars"])
    for theme, (lo, hi) in spec["recurrence"].items():
        if not lo <= counts.get(theme, 0) <= hi:
            violations.append(
                f"主题 {theme} 复现次数 {counts.get(theme, 0)} 超出约束 [{lo},{hi}]")

    total_bars = sum(bars.values())
    divides = total_bars % spec["phrase_bars"] == 0
    if divides:
        violations.append(
            f"乐句长度 {spec['phrase_bars']} 小节整除总片长 {total_bars} 小节，"
            "结构退化为整段循环（被约束禁止）")

    dur_err = abs(delivery_sec - target)
    lo_lim, hi_lim = spec["tempo_limits"]
    if not lo_lim <= scale <= hi_lim:
        violations.append(f"所需速度缩放 {scale:.3f} 超出允许范围 [{lo_lim},{hi_lim}]，"
                          "必须改变结构或放宽约束")

    time_budget = max(0.25, max((e.tolerance for e in spec["emphases"]), default=0.25))
    if dur_err > time_budget and strategy == "section_replace":
        violations.append(f"替换乐段后交付 {delivery_sec:.2f}s 与目标 {target:.2f}s "
                          f"相差 {dur_err:.2f}s，超出容限 {time_budget:.2f}s")
    score = norm_err + 0.02 * dur_err / target
    return Candidate(bars, total_bars, scale, delivery_sec, nominal_sec,
                     max_err, dur_err, emph_rows, counts, divides,
                     violations, score, tmap, segs)


def _enumerate_bar_combos(spec: dict) -> list[dict[str, int]]:
    options = []
    for kind in CANONICAL_ORDER:
        if kind not in spec["allowed_sections"]:
            options.append([(kind, 0)])
            continue
        choices = spec["bar_ranges"].get(kind) or (0,)
        if kind in ("development", "recap"):
            choices = [c for c in choices if c > 0]
        options.append([(kind, c) for c in choices])
    combos = []
    for prod in itertools.product(*options):
        bars = dict(prod)
        if bars.get("development", 0) <= 0 or bars.get("recap", 0) <= 0:
            continue
        combos.append(bars)
    return combos


def _build_plan(cand: Candidate, spec: dict, strategy: str,
                ranked: list[Candidate]) -> tuple[Plan, object, TempoMap]:
    sections, cursor_beat = [], 0.0
    for seg in cand.segments:
        n_bars = int(round(seg.beats / BEATS_PER_BAR))
        sections.append(SectionChoice(kind=seg.name, bars=n_bars,
                                      start_beat=cursor_beat, start_sec=seg.start_sec,
                                      bpm_start=seg.bpm_start, bpm_end=seg.bpm_end))
        cursor_beat += seg.beats
    scheduled = build_schedule(sections, spec["materials"], spec["phrase_bars"])
    for sc in sections:
        sc.statements = {
            t: sum(1 for ev in scheduled.events
                   if ev.section == sc.kind and ev.voice == "melody")
            for t in ("A", "B")}
    tmap = cand.tempo_map
    plan = Plan(
        sections=sections,
        total_bars=cand.total_bars,
        total_beats=tmap.total_beats,
        bpm_scale=cand.scale,
        target_sec=spec["target_sec"],
        delivery_sec=cand.delivery_sec,
        nominal_sec=cand.nominal_sec,
        emphasis_events=cand.emphasis,
        phrase_bars=spec["phrase_bars"],
        phrase_divides=cand.phrase_divides,
        theme_counts=cand.theme_counts,
        tail_policy=spec["tail_policy"],
        score={"strategy": strategy, "value": round(cand.score, 5),
               "max_emphasis_error_sec": round(cand.max_emph_err, 4),
               "duration_error_sec": round(cand.duration_err, 4),
               "candidate_count": len(ranked)},
        feasible=not cand.violations,
        violations=cand.violations,
        schedule=[{
            "voice": ev.voice, "pitch": ev.pitch,
            "start_beat": round(ev.start_beat, 4),
            "start_sec": round(tmap.time_at_beat(ev.start_beat), 4),
            "dur_beats": round(ev.dur_beats, 4),
            "dur_sec": round(tmap.time_at_beat(ev.start_beat + ev.dur_beats)
                             - tmap.time_at_beat(ev.start_beat), 4),
            "vel": round(ev.vel, 3), "section": ev.section, "accent": ev.accent,
        } for ev in scheduled.events],
        tempo_curve=tmap.curve_samples(0.5),
        comparison={"candidates_ranked": [
            {"bars": c.bars, "total_bars": c.total_bars, "scale": round(c.scale, 4),
             "delivery_sec": round(c.delivery_sec, 3),
             "max_emphasis_error_sec": round(c.max_emph_err, 3),
             "feasible": not c.violations, "violations": c.violations}
            for c in ranked[:8]]},
    )
    return plan, scheduled, tmap


def plan_structure(raw_spec: dict, strategy: str = "tempo_map",
                   ritardando: bool = True) -> tuple[Plan, object, TempoMap]:
    """主入口：返回 (Plan, ScheduledScore, TempoMap)。不可行时仍返回最优候选与违规说明。"""
    spec = load_spec(raw_spec)
    candidates = sorted(
        (c for bars in _enumerate_bar_combos(spec)
         if (c := _evaluate(bars, spec, strategy)) is not None),
        key=lambda c: c.score)
    best = candidates[0]
    if ritardando and best.segments[-1].name == "coda":
        # 末段渐慢：先取段内形状（末速=起速*0.85），再按"目标交付时长"精确反解
        # 统一缩放系数，保证渐慢只改变曲线形状而不产生时长漂移。
        shaped = ramp_toward(best.segments, 0.85)
        shaped_total = TempoMap(shaped).total_sec
        release = spec["release_sec"] if spec["tail_policy"] == "include_release" else 0.0
        target_sound = spec["target_sec"] - release
        exact_scale = shaped_total / max(target_sound, 1e-6)
        corrected = [Segment(s.name, s.beats, s.bpm_start * exact_scale,
                             s.bpm_end * exact_scale) for s in shaped]
        # 有效缩放 = 名义总时长 / 目标发声时长（与渐慢形状无关，用于速度限制判定）
        effective_scale = best.nominal_sec / max(target_sound, 1e-6)
        best = _evaluate(best.bars, spec, strategy, segments=corrected,
                         preset_scale=effective_scale)
    return _build_plan(best, spec, strategy, candidates)


def compare_strategies(raw_spec: dict) -> dict:
    """同一规格下比较改速度图与替换乐段两条路线对节奏与主题再现的影响。"""
    tempo_plan, _, _ = plan_structure(raw_spec, "tempo_map")
    replace_plan, _, _ = plan_structure(raw_spec, "section_replace", ritardando=False)

    def pack(p: Plan) -> dict:
        return {
            "feasible": p.feasible, "violations": p.violations,
            "bars": {s.kind: s.bars for s in p.sections},
            "total_bars": p.total_bars, "bpm_scale": round(p.bpm_scale, 4),
            "delivery_sec": round(p.delivery_sec, 3),
            "theme_counts": p.theme_counts,
            "tempo_range_bpm": [round(min(x["bpm"] for x in p.tempo_curve), 2),
                                round(max(x["bpm"] for x in p.tempo_curve), 2)],
            "emphasis": p.emphasis_events,
        }

    bars_t = {s.kind: s.bars for s in tempo_plan.sections}
    bars_r = {s.kind: s.bars for s in replace_plan.sections}
    changed = [k for k in set(bars_t) | set(bars_r)
               if bars_t.get(k, 0) != bars_r.get(k, 0)]
    te = sum(abs(e["error"]) for e in tempo_plan.emphasis_events if e["error"] is not None)
    re_ = sum(abs(e["error"]) for e in replace_plan.emphasis_events if e["error"] is not None)
    return {
        "tempo_map": pack(tempo_plan),
        "section_replace": pack(replace_plan),
        "summary": [
            f"改速度图：保持 {tempo_plan.total_bars} 小节结构，整体缩放 "
            f"x{tempo_plan.bpm_scale:.3f}，主题复现 {tempo_plan.theme_counts} 不变。",
            f"替换乐段：速度保持名义值，变化的段落 {changed or '无'}，"
            f"主题复现变为 {replace_plan.theme_counts}，直接影响再现关系。",
            f"强调时点绝对误差合计：改速度 {te:.2f}s vs 换乐段 {re_:.2f}s。",
        ],
    }
