"""材料规则的可运行约束检查（曲式资料检查的单一事实来源）。

把 README「核心约束」与 docs/USAGE.md 的材料规则编译成可执行检查，
规划器、校验脚本与界面共用同一套规则，避免各处手写判断产生伪报。

诊断策略（经实测选定，见 docs/USAGE.md 第 6 节）：
  规格的 bar_ranges 网格只有十几到一百多个组合，前向枚举 + 全约束检查
  单次运行 < 3ms，且能穷举全部候选；因此以「材料约束可满足性」前向检查为主
  诊断路径，渲染 PCM 能量峰逆查（renderer.verify_render）仅作为演奏级佐证。

规则编号与文档一一对应：
  R1 乐句长度不得整除总小节（禁止整段循环退化）
  R2 禁止单段循环拉满：发展/回归必备，发展段至少 B、A 两个对比乐句，
     高潮锁定在发展段最后一个乐句的强拍，收束段末尾固定 I–V–I 终止
  R3 严格时长：交付时长由小节、节拍与速度曲线共同计算，速度缩放须在 tempo_limits 内
  R4 主题复现次数（含零头半乐句）须落入 recurrence 区间
  R5 强调时点绑定真实事件，落点须在容差内且顺序不冲突
  R6 尾音口径须显式声明且交付长度与口径一致
  R7 最后乐句须有材料支持的结束（收束 I–V–I），不得裁尾
"""
from __future__ import annotations

import copy
import itertools
from dataclasses import dataclass, field
from typing import Any, Callable

from .model import BEATS_PER_BAR, CANONICAL_ORDER, Material, load_spec
from .planner import (_enumerate_bar_combos, _evaluate, _event_beats,
                     _theme_counts)
from .scheduler import _development_plan
from .timeline import TempoMap

# 规则编号 -> 中文名（文档与诊断报告共用）
RULE_NAMES = {
    "R1": "乐句非整除（禁止整段循环退化）",
    "R2": "真实段落与锁定高潮（禁止单段循环拉满）",
    "R3": "严格时长与速度缩放区间",
    "R4": "主题复现次数区间（含零头半乐句）",
    "R5": "强调时点容差与顺序",
    "R6": "尾音口径与交付长度一致",
    "R7": "收束 I–V–I 材料终止（禁止裁尾）",
}


@dataclass
class RuleViolation:
    rule: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConstraintReport:
    feasible: bool
    violations: list[RuleViolation]
    # 每条规则在全部候选上的命中次数（用于解释规则覆盖面，不允许伪报）
    rule_hits: dict[str, int]
    candidate_count: int
    feasible_candidate_count: int
    best_bars: dict[str, int] | None
    trace: list[dict[str, Any]]          # 可生成的规划轨迹（逐候选）

    def to_dict(self) -> dict[str, Any]:
        return {
            "feasible": self.feasible,
            "violations": [{"rule": v.rule, "rule_name": RULE_NAMES[v.rule],
                            "message": v.message, "evidence": v.evidence}
                           for v in self.violations],
            "rule_hits": self.rule_hits,
            "candidate_count": self.candidate_count,
            "feasible_candidate_count": self.feasible_candidate_count,
            "best_bars": self.best_bars,
            "trace": self.trace,
        }


# ---------------------------------------------------------------------------
# 材料本身的静态检查（不依赖枚举）：自制主题必须真的能演奏
# ---------------------------------------------------------------------------

def check_materials(spec: dict[str, Any]) -> list[RuleViolation]:
    """检查自制/内置材料的静态合法性，违规归到 R2（材料支持）。"""
    out: list[RuleViolation] = []
    for key, mat in spec["materials"].items():
        span = mat.phrase_bars * BEATS_PER_BAR
        if mat.phrase_bars <= 0:
            out.append(RuleViolation("R2", f"主题 {key} 的 phrase_bars 必须为正整数",
                                     {"theme": key}))
            continue
        if not mat.melody:
            out.append(RuleViolation("R2", f"主题 {key} 缺少旋律音符，无法陈述",
                                     {"theme": key}))
        for voice, notes in (("melody", mat.melody), ("bass", mat.bass)):
            for n in notes:
                if n.start < 0 or n.dur <= 0:
                    out.append(RuleViolation("R2",
                                             f"主题 {key} 的 {voice} 存在起点/时长非法音符",
                                             {"theme": key, "voice": voice,
                                              "start": n.start, "dur": n.dur}))
                if n.start + n.dur > span + 1e-9:
                    out.append(RuleViolation(
                        "R2",
                        f"主题 {key} 的 {voice} 音符超出 {mat.phrase_bars} 小节乐句边界",
                        {"theme": key, "voice": voice, "start": n.start,
                         "dur": n.dur, "phrase_span_beats": span}))
        if mat.chords:
            last_cb = max(c[0] for c in mat.chords)
            if last_cb >= span:
                out.append(RuleViolation(
                    "R2", f"主题 {key} 和弦起点超出乐句边界",
                    {"theme": key, "last_chord_beat": last_cb,
                     "phrase_span_beats": span}))
        else:
            out.append(RuleViolation("R2", f"主题 {key} 缺少和弦标签，和声垫无材料可排",
                                     {"theme": key}))
    return out


# ---------------------------------------------------------------------------
# 单候选的规则级检查：与 scheduler 的真实调度口径保持一致
# ---------------------------------------------------------------------------

def _theme_counts_actual(bars: dict[str, int], phrase_bars: int) -> dict[str, int]:
    """按 scheduler 真实乐句口径统计主题陈述数。

    与 planner._theme_counts 的差别：零头小节在 intro/recap/coda 主体里也会被
    调度成一个半乐句材料陈述（见 scheduler.build_schedule），必须计入；
    收束段末尾 2 小节是 I–V–I 终止式而非主题材料，不计入。
    """
    counts = {"A": 0, "B": 0}

    def phrases_for(n_bars: int) -> int:
        if n_bars <= 0:
            return 0
        full, rem = divmod(n_bars, phrase_bars)
        return full + (1 if rem else 0)

    counts["A"] += phrases_for(bars.get("intro", 0))
    dev = bars.get("development", 0)
    if dev:
        for theme, _transform, _pb in _development_plan(dev, phrase_bars):
            counts[theme] = counts.get(theme, 0) + 1
    counts["A"] += phrases_for(bars.get("recap", 0))
    coda_body = max(0, bars.get("coda", 0) - 2)   # 去掉末尾 I–V–I 终止两小节
    counts["A"] += phrases_for(coda_body)
    return counts


def check_candidate(bars: dict[str, int], spec: dict[str, Any],
                    strategy: str = "tempo_map") -> list[RuleViolation]:
    """对单个 bar 组合执行 R1–R7 的规则检查，返回结构化违规（无违规即可行）。"""
    cand = _evaluate(bars, spec, strategy)
    if cand is None:
        return [RuleViolation("R2", "该网格组合没有任何有效段落", {"bars": bars})]
    violations: list[RuleViolation] = []
    total_bars = cand.total_bars
    phrase_bars = spec["phrase_bars"]

    # R1 乐句非整除
    if total_bars % phrase_bars == 0:
        violations.append(RuleViolation(
            "R1", f"乐句长度 {phrase_bars} 小节整除总片长 {total_bars} 小节，"
                  "结构退化为整段循环",
            {"total_bars": total_bars, "phrase_bars": phrase_bars}))

    # R2 必备段落、发展段对比乐句与锁定高潮
    if bars.get("development", 0) <= 0 or bars.get("recap", 0) <= 0:
        violations.append(RuleViolation(
            "R2", "发展段与回归段不可省略，禁止以单段循环拉满时长",
            {"bars": bars}))
    if bars.get("development", 0) > 0:
        shape = _development_plan(bars["development"], phrase_bars)
        themes = {t for t, _, _ in shape}
        if not ({"A", "B"} <= themes):
            violations.append(RuleViolation(
                "R2", "发展段至少包含 B→A 两个对比乐句",
                {"development_plan": [(t, v, pb) for t, v, pb in shape]}))
        # 高潮锁定：发展段最后一个乐句的强拍（planner._event_beats 同口径）
        event_beats = _event_beats(bars, phrase_bars)
        climax_beat = event_beats.get("climax")
        dev_start = next(b for i, (k, b) in enumerate(
            zip(CANONICAL_ORDER, _prefix_bars(bars))) if k == "development")
        last_phrase_offset = sum(pb for _, _, pb in shape[:-1]) * BEATS_PER_BAR
        if climax_beat is None or abs(climax_beat - (dev_start + last_phrase_offset)) > 1e-9:
            violations.append(RuleViolation(
                "R2", "高潮必须锁定在发展段最后一个乐句的强拍",
                {"climax_beat": climax_beat,
                 "expected_beat": dev_start + last_phrase_offset}))

    # R3 严格时长与速度缩放区间
    lo_lim, hi_lim = spec["tempo_limits"]
    if not lo_lim <= cand.scale <= hi_lim:
        violations.append(RuleViolation(
            "R3", f"所需速度缩放 {cand.scale:.3f} 超出允许区间 [{lo_lim},{hi_lim}]，"
                  "必须改变结构",
            {"scale": round(cand.scale, 4), "tempo_limits": [lo_lim, hi_lim],
             "delivery_sec": round(cand.delivery_sec, 4),
             "target_sec": spec["target_sec"]}))
    dur_err = abs(cand.delivery_sec - spec["target_sec"])
    if dur_err > max(0.25, max((e.tolerance for e in spec["emphases"]), default=0.25)):
        violations.append(RuleViolation(
            "R3", f"交付时长 {cand.delivery_sec:.3f}s 偏离目标 {spec['target_sec']}s "
                  f"达 {dur_err:.3f}s",
            {"delivery_sec": round(cand.delivery_sec, 4),
             "target_sec": spec["target_sec"], "error_sec": round(dur_err, 4)}))

    # R4 主题复现（真实调度口径，含零头半乐句）
    actual_counts = _theme_counts_actual(bars, phrase_bars)
    for theme, (lo, hi) in spec["recurrence"].items():
        n = actual_counts.get(theme, 0)
        if not lo <= n <= hi:
            violations.append(RuleViolation(
                "R4", f"主题 {theme} 实际陈述 {n} 次（含零头半乐句），"
                      f"超出约束区间 [{lo},{hi}]",
                {"theme": theme, "actual": n, "range": [lo, hi],
                 "planner_grid_count": cand.theme_counts.get(theme, 0)}))

    # R5 强调时点（复用 planner 的真实事件映射，保证同一口径）
    for row in cand.emphasis:
        if row["actual"] is None:
            violations.append(RuleViolation(
                "R5", f"强调 {row.get('label') or row['role']} 绑定的段落未选入，无真实事件",
                {"role": row["role"], "label": row.get("label", "")}))
        elif not row["matched"]:
            violations.append(RuleViolation(
                "R5", f"强调 {row.get('label') or row['role']} 落点 "
                      f"{row['actual']}s 偏离目标 {row['target']}s，"
                      f"误差 {row['error']}s 超过容差 ±{row['tolerance']}s",
                dict(row)))
    ordered = [r for r in cand.emphasis if r["actual"] is not None]
    for r1, r2 in zip(ordered, ordered[1:]):
        if r2["actual"] < r1["actual"] - 1e-9:
            violations.append(RuleViolation(
                "R5", f"强调顺序冲突：{r1['role']} 晚于 {r2['role']}",
                {"first": r1["role"], "second": r2["role"]}))

    # R6 尾音口径：planner 已按口径反解，这里核对交付长度与口径自洽
    release = spec["release_sec"] if spec["tail_policy"] == "include_release" else 0.0
    expect_delivery = cand.tempo_map.total_sec + release
    if abs(expect_delivery - cand.delivery_sec) > 1e-6:
        violations.append(RuleViolation(
            "R6", f"尾音口径 {spec['tail_policy']} 下交付长度计算不自洽",
            {"expected": round(expect_delivery, 4),
             "actual": round(cand.delivery_sec, 4),
             "tail_policy": spec["tail_policy"]}))

    # R7 收束材料终止：含收束段时末尾两小节必须是 I–V–I（scheduler 固定写入）
    if bars.get("coda", 0) > 0:
        if bars["coda"] < 2:
            violations.append(RuleViolation(
                "R7", "收束段不足 2 小节，无法容纳 I–V–I 正格终止",
                {"coda_bars": bars["coda"]}))
    return violations


def _prefix_bars(bars: dict[str, int]) -> list[float]:
    out, cur = [], 0.0
    for k in CANONICAL_ORDER:
        out.append(cur)
        cur += bars.get(k, 0) * BEATS_PER_BAR
    return out


# ---------------------------------------------------------------------------
# 全规格检查 + 规划轨迹
# ---------------------------------------------------------------------------

def check_spec(raw_spec: dict[str, Any], strategy: str = "tempo_map",
               trace: bool = True) -> ConstraintReport:
    """前向枚举全部 bar 网格组合，输出规则级检查报告与逐候选规划轨迹。"""
    spec = load_spec(raw_spec)
    combos = _enumerate_bar_combos(spec)
    rule_hits: dict[str, int] = {r: 0 for r in RULE_NAMES}
    traces: list[dict[str, Any]] = []
    feasible_count = 0
    best_bars = None
    best_score = float("inf")
    first_violations: list[RuleViolation] = []

    for bars in combos:
        violations = check_materials(spec) + check_candidate(bars, spec, strategy)
        cand = _evaluate(bars, spec, strategy)
        if not violations:
            feasible_count += 1
        for v in violations:
            rule_hits[v.rule] = rule_hits.get(v.rule, 0) + 1
        if trace and cand is not None:
            traces.append({
                "bars": bars, "total_bars": cand.total_bars,
                "scale": round(cand.scale, 4),
                "delivery_sec": round(cand.delivery_sec, 4),
                "feasible": not violations,
                "violated_rules": sorted({v.rule for v in violations}),
                "violations": [f"[{v.rule}] {v.message}" for v in violations],
                "theme_counts_actual": _theme_counts_actual(bars, spec["phrase_bars"]),
            })
        if cand is not None and cand.score < best_score:
            best_score, best_bars = cand.score, bars
            first_violations = violations

    return ConstraintReport(
        feasible=feasible_count > 0,
        violations=first_violations,
        rule_hits=rule_hits,
        candidate_count=len(combos),
        feasible_candidate_count=feasible_count,
        best_bars=best_bars,
        trace=traces if trace else [],
    )


# ---------------------------------------------------------------------------
# 最小冲突集：删掉哪些规则后规格重新可满足（删除唯一过渡约束的可满足性实验）
# ---------------------------------------------------------------------------

# 可独立松弛的规则组：R5(强调) / R1(整除) / R4(复现) / R3(速度区间)
RELAXABLE_RULES = ("R5", "R1", "R4", "R3")


def _relax(raw_spec: dict[str, Any], rules: tuple[str, ...]) -> dict[str, Any]:
    spec = copy.deepcopy(raw_spec)
    if "R5" in rules:
        spec["emphases"] = []
    if "R1" in rules:
        spec["phrase_bars"] = 1           # 1 小节乐句永不整除总小节
    if "R4" in rules:
        spec["recurrence"] = {"A": [0, 999], "B": [0, 999]}
    if "R3" in rules:
        spec["tempo_limits"] = [0.01, 100.0]
    return spec


def _satisfiable(raw_spec: dict[str, Any], strategy: str) -> bool:
    report = check_spec(raw_spec, strategy, trace=False)
    return report.feasible


def minimal_conflict_sets(raw_spec: dict[str, Any], strategy: str = "tempo_map",
                          max_size: int | None = None
                          ) -> list[dict[str, Any]]:
    """返回最小冲突集列表：删除集合内规则后可满足，且其任何真子集仍不可满足。

    输出即「删除唯一过渡句后的最小冲突集合」；把规则补回后重新调用
    check_spec 应恢复原始可行性结论（见 tests 与 validate_plan.py）。
    """
    if max_size is None:
        max_size = len(RELAXABLE_RULES)
    results: list[dict[str, Any]] = []
    for size in range(1, max_size + 1):
        for combo in itertools.combinations(RELAXABLE_RULES, size):
            if not _satisfiable(_relax(raw_spec, combo), strategy):
                continue
            # 最小性：每个真子集松弛后仍不可满足
            minimal = all(
                not _satisfiable(_relax(raw_spec, sub), strategy)
                for k in range(size)
                for sub in itertools.combinations(combo, k))
            if minimal:
                results.append({
                    "rules": list(combo),
                    "rule_names": [RULE_NAMES[r] for r in combo],
                    "feasible_after_relax": True,
                    "minimal": True,
                })
    return results


def diagnose(raw_spec: dict[str, Any], strategy: str = "tempo_map") -> dict[str, Any]:
    """不可行目标的明确诊断：规则命中、最小冲突集与补回复核。"""
    report = check_spec(raw_spec, strategy)
    conflicts = [] if report.feasible else minimal_conflict_sets(raw_spec, strategy)
    return {
        "feasible": report.feasible,
        "candidate_count": report.candidate_count,
        "feasible_candidate_count": report.feasible_candidate_count,
        "rule_hits": report.rule_hits,
        "best_bars": report.best_bars,
        "violations": report.to_dict()["violations"],
        "minimal_conflict_sets": conflicts,
        "restore_check": _restore_check(raw_spec, conflicts, strategy),
    }


def _restore_check(raw_spec: dict[str, Any], conflicts: list[dict[str, Any]],
                   strategy: str) -> dict[str, Any]:
    """补回被删规则后重新检查：可行性必须恢复到删除前的结论（防止松弛泄漏）。"""
    baseline = check_spec(raw_spec, strategy, trace=False).feasible
    restored = []
    for c in conflicts:
        relaxed = _relax(raw_spec, tuple(c["rules"]))
        after_relax = check_spec(relaxed, strategy, trace=False).feasible
        after_restore = check_spec(copy.deepcopy(raw_spec), strategy,
                                   trace=False).feasible
        restored.append({
            "rules": c["rules"],
            "feasible_when_relaxed": after_relax,
            "feasible_after_restore": after_restore,
            "restore_matches_baseline": after_restore == baseline,
        })
    return {"baseline_feasible": baseline, "checks": restored}
