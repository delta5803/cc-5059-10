#!/usr/bin/env python3
"""可复算构建：重新生成三个样例的全部交付物（谱面/JSON/真实音频/核验报告）。

用法：python build_examples.py
仅依赖 Python 3.11 标准库，输出到 build/<样例名>/。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shortscore.planner import compare_strategies, plan_structure
from shortscore.renderer import render_wav
from shortscore.scoreio import write_json_score, write_musicxml

ROOT = os.path.dirname(os.path.abspath(__file__))
EXAMPLES = [
    ("1_feasible", "长度可行"),
    ("2_must_change_tempo", "必须改变速度"),
    ("3_emphasis_conflict", "无法同时满足强调位置"),
]


def main() -> int:
    summary = []
    for name, cn in EXAMPLES:
        spec_path = os.path.join(ROOT, "examples", name + ".json")
        with open(spec_path, encoding="utf-8") as f:
            spec = json.load(f)
        out_dir = os.path.join(ROOT, "build", name)
        os.makedirs(out_dir, exist_ok=True)

        plan, scheduled, tmap = plan_structure(spec)
        wav_path = os.path.join(out_dir, "preview.wav")
        render = render_wav(
            plan.schedule, tmap, wav_path,
            sample_rate=spec.get("sample_rate", 44100),
            tail_policy=spec.get("tail_policy", "include_release"),
            release_sec=spec.get("release_sec", 0.45),
            structural_events=scheduled.structural,
            emphases=spec.get("emphases", []))
        write_musicxml(plan, os.path.join(out_dir, "score.musicxml"),
                       spec.get("title", name))
        write_json_score(plan, os.path.join(out_dir, "score.json"),
                         render={"wav": os.path.abspath(wav_path),
                                 "rendered_sec": render.rendered_sec,
                                 "sample_rate": render.sample_rate,
                                 "verification": render.verification})
        comparison = compare_strategies(spec)
        with open(os.path.join(out_dir, "strategy_comparison.json"), "w",
                  encoding="utf-8") as f:
            json.dump(comparison, f, ensure_ascii=False, indent=2)
        with open(os.path.join(out_dir, "structure_rationale.md"), "w",
                  encoding="utf-8") as f:
            f.write(_rationale_md(name, cn, spec, plan, render, comparison))
        summary.append((name, cn, plan.feasible, plan.total_bars,
                        round(plan.delivery_sec, 2), render.rendered_sec))
        print(f"[{'OK' if plan.feasible else '受限'}] {name} -> {out_dir}")

    print("\n样例        可行  小节  目标交付  实际渲染")
    for name, cn, ok, bars, deliver, rendered in summary:
        print(f"{name:<26} {ok!s:<5} {bars:<4} {deliver:<8} {round(rendered,2)}")
    return 0


def _rationale_md(name, cn, spec, plan, render, comparison) -> str:
    lines = [
        f"# 段落依据：{spec.get('title', name)}（{cn}）",
        "",
        f"- 目标时长：{spec['target_sec']}s；尾音策略：`{plan.tail_policy}`"
        f"（释放 {spec.get('release_sec', 0.45)}s "
        f"{'计入' if plan.tail_policy == 'include_release' else '不计入'}交付长度）",
        f"- 总小节：{plan.total_bars}；乐句长度：{plan.phrase_bars} 小节；"
        f"乐句整除总片长：**{plan.phrase_divides}**（约束要求为 False）",
        f"- 速度缩放：x{round(plan.bpm_scale, 4)}；交付时长：{round(plan.delivery_sec, 3)}s；"
        f"真实渲染：{round(render.rendered_sec, 3)}s",
        f"- 主题复现次数：{plan.theme_counts}（约束 {spec.get('recurrence')}）",
        "",
        "## 段落安排",
    ]
    for s in plan.sections:
        lines.append(
            f"- **{s.kind}** {s.bars} 小节｜起 {round(s.start_sec, 2)}s｜"
            f"BPM {round(s.bpm_start, 2)}→{round(s.bpm_end, 2)}"
            f"{'｜' + s.variant if s.variant else ''}")
    lines += ["", "## 强调时点（真实渲染 PCM 核验）"]
    for row in render.verification:
        if "强调" in row["check"]:
            lines.append(
                f"- [{'PASS' if row['pass'] else 'FAIL'}] {row['check']}："
                f"目标 {row.get('target_sec')}s，预测事件 {row.get('predicted_event_sec')}s，"
                f"实测能量峰 {row.get('measured_peak_sec')}s，"
                f"误差 {row.get('peak_vs_target_error_sec')}s，容差 ±{row.get('tolerance_sec')}s")
    lines += ["", "## 违规与误差来源（不可行样例在此显式列出）"]
    lines += [f"- {v}" for v in plan.violations] or ["- 无"]
    # 根因：把所有候选结构里冲突事件对的最小可达间距列出来
    if not plan.feasible:
        lines += ["", "### 根因分析：事件最小可达间距"]
        from shortscore.planner import (_enumerate_bar_combos, _event_beats,
                                        _evaluate, load_spec)
        spec_norm = load_spec(spec)
        ordered = [e for e in spec_norm["emphases"]]
        pairs = list(zip(ordered, ordered[1:]))
        for e1, e2 in pairs:
            gaps = []
            for bars in _enumerate_bar_combos(spec_norm):
                cand = _evaluate(bars, spec_norm, "tempo_map")
                if not cand:
                    continue
                eb = _event_beats(bars, spec_norm["phrase_bars"])
                if e1.role in eb and e2.role in eb:
                    g = abs(cand.tempo_map.time_at_beat(eb[e2.role])
                            - cand.tempo_map.time_at_beat(eb[e1.role]))
                    gaps.append(g)
            if gaps:
                lines.append(
                    f"- `{e1.role}` 与 `{e2.role}` 目标间距 "
                    f"{abs(e2.time - e1.time):.2f}s（容差和 ±{e1.tolerance + e2.tolerance:.2f}s），"
                    f"但全部 {len(gaps)} 个候选结构中二者最小可达间距为 {min(gaps):.2f}s，"
                    f"结构上不可能同时命中。")
    lines += ["", "## 改速度图 vs 替换乐段"]
    lines += [f"- {s}" for s in comparison["summary"]]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
