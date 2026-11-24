#!/usr/bin/env python3
"""曲式资料检查与维护校验（文档样例的可复现入口）。

职责：
  1. 对每个规格跑材料约束检查（shortscore.constraints，R1–R7 单一事实来源），
     输出逐候选规划轨迹、规则命中与最小冲突集；
  2. 实际渲染 WAV 并读回 PCM 核验（严格时长、锁定高潮、段落信号、强调能量峰）；
  3. 用段落时长与主题采用（真实 scheduler 乐句口径）核对文档与实现，不允许伪报；
  4. 不可行目标给出明确诊断示例（最小冲突集 + 删除/补回规则的可满足性实验）。

设计为在干净目录可独立运行：只读取 examples/ 与包代码，不依赖任何手工修改的数据。

用法：
  python3 validate_plan.py                      # 校验全部内置样例
  python3 validate_plan.py examples/1_feasible.json
  python3 validate_plan.py --out /tmp/audit     # 写出 JSON 审计报告
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shortscore.constraints import RULE_NAMES, check_spec, diagnose
from shortscore.planner import plan_structure
from shortscore.renderer import render_wav
from shortscore.scheduler import _development_plan
from shortscore.model import BEATS_PER_BAR

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SPECS = [
    os.path.join(ROOT, "examples", f"{n}.json")
    for n in ("1_feasible", "2_must_change_tempo", "3_emphasis_conflict")
]


def _section_duration_evidence(plan, tmap) -> list[dict]:
    """段落时长证据：每段起讫秒数由 TempoMap 实时计算，与界面/谱面同源。"""
    out = []
    for s in plan.sections:
        end_beat = s.start_beat + s.bars * BEATS_PER_BAR
        end_sec = tmap.time_at_beat(end_beat)
        out.append({
            "section": s.kind, "bars": s.bars,
            "start_sec": round(s.start_sec, 4), "end_sec": round(end_sec, 4),
            "duration_sec": round(end_sec - s.start_sec, 4),
            "bpm_start": round(s.bpm_start, 3), "bpm_end": round(s.bpm_end, 3),
            "statements": s.statements, "variant": s.variant,
        })
    return out


def _theme_adoption_evidence(plan) -> dict:
    """主题采用证据：发展段乐句计划 + 全曲主题陈述计数（实现口径）。"""
    dev = next((s for s in plan.sections if s.kind == "development"), None)
    return {
        "development_plan": dev.variant if dev else None,
        "development_has_A_and_B": _has_both_themes(dev) if dev else False,
        "theme_counts": plan.theme_counts,
        "section_statements": {s.kind: s.statements for s in plan.sections},
    }


def _has_both_themes(section) -> bool:
    if not section or not section.variant:
        return False
    themes = {chunk.split(":", 1)[0] for chunk in section.variant.split("→")}
    return {"A", "B"} <= themes


def _render_evidence(raw: dict, plan, scheduled, tmap) -> dict:
    """实际渲染并读回 PCM：可播放性 + 严格时长/高潮/强调的执行证据。"""
    with tempfile.TemporaryDirectory() as tmp:
        wav_path = os.path.join(tmp, "preview.wav")
        result = render_wav(
            plan.schedule, tmap, wav_path,
            sample_rate=raw.get("sample_rate", 44100),
            tail_policy=raw.get("tail_policy", "include_release"),
            release_sec=raw.get("release_sec", 0.45),
            structural_events=scheduled.structural,
            emphases=raw.get("emphases", []))
        with wave.open(wav_path, "rb") as wf:
            params = wf.getparams()
            frames = wf.getnframes()
    return {
        "wav_playable": params.nchannels == 1 and params.framerate > 0 and frames > 0,
        "sample_rate": result.sample_rate,
        "frames": frames,
        "rendered_sec": round(result.rendered_sec, 4),
        "delivery_sec": round(plan.delivery_sec, 4),
        "target_sec": raw["target_sec"],
        "strict_duration_pass": all(
            r["pass"] for r in result.verification
            if r["check"].startswith("交付长度")),
        "climax_locked_pass": _climax_pass(result, scheduled, tmap),
        "verification": result.verification,
    }


def _climax_pass(result, scheduled, tmap) -> bool:
    """高潮锁定证据：scheduler 标记的 climax 拍点必须在 PCM 上形成超均值能量峰。"""
    climax = next((e for e in scheduled.structural if e["kind"] == "climax"), None)
    if climax is None:
        return False
    row = next((r for r in result.verification
                if r["check"] == "结构事件能量核验：climax"), None)
    return bool(row and row["pass"])


def validate_spec(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    report = check_spec(raw)
    diagnosis = diagnose(raw)
    plan, scheduled, tmap = plan_structure(raw)

    # 文档/实现一致性：约束模块结论必须与 planner 可行性结论相同（防分叉、防伪报）
    consistency = {
        "constraints_feasible": report.feasible,
        "planner_feasible": plan.feasible,
        "agree": report.feasible == plan.feasible,
    }
    render = _render_evidence(raw, plan, scheduled, tmap)
    audit = {
        "spec": os.path.basename(path),
        "title": raw.get("title"),
        "consistency": consistency,
        "rule_hits": {k: v for k, v in report.rule_hits.items() if v},
        "rule_names": RULE_NAMES,
        "candidate_count": report.candidate_count,
        "feasible_candidate_count": report.feasible_candidate_count,
        "planning_trace": report.trace,
        "section_duration_evidence": _section_duration_evidence(plan, tmap),
        "theme_adoption_evidence": _theme_adoption_evidence(plan),
        "render_evidence": render,
        "violations": diagnosis["violations"],
        "minimal_conflict_sets": diagnosis["minimal_conflict_sets"],
        "restore_check": diagnosis["restore_check"],
    }
    audit["passed"] = _evaluate_pass(audit)
    return audit


def _evaluate_pass(audit: dict) -> bool:
    """通过条件：结论一致、可播放、严格时长与高潮锁定通过；不可行样例须有最小冲突集。"""
    if not audit["consistency"]["agree"]:
        return False
    re_ = audit["render_evidence"]
    if not (re_["wav_playable"] and re_["strict_duration_pass"]
            and re_["climax_locked_pass"]):
        return False
    if audit["consistency"]["planner_feasible"]:
        return not audit["violations"]
    return bool(audit["minimal_conflict_sets"]) and all(
        c["restore_matches_baseline"]
        for c in audit["restore_check"]["checks"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="曲式资料检查与维护校验")
    parser.add_argument("specs", nargs="*", help="规格 JSON；缺省校验全部内置样例")
    parser.add_argument("--out", help="把完整 JSON 审计报告写到该目录")
    args = parser.parse_args(argv)

    paths = args.specs or DEFAULT_SPECS
    audits = []
    failures = 0
    for path in paths:
        audit = validate_spec(path)
        audits.append(audit)
        ok = audit["passed"]
        failures += 0 if ok else 1
        print(f"[{'PASS' if ok else 'FAIL'}] {audit['spec']} "
              f"feasible={audit['consistency']['planner_feasible']} "
              f"candidates={audit['candidate_count']} "
              f"render={audit['render_evidence']['rendered_sec']}s")
        for ev in audit["section_duration_evidence"]:
            print(f"    {ev['section']:<11} {ev['bars']}小节 "
                  f"{ev['duration_sec']:6.3f}s 陈述{ev['statements']} "
                  f"{ev['variant'] or ''}")
        if not ok:
            for v in audit["violations"]:
                print(f"      ! [{v['rule']}] {v['message']}")
        for c in audit["minimal_conflict_sets"]:
            print(f"      最小冲突集: {c['rules']} -> {c['rule_names']}")

    if args.out:
        os.makedirs(args.out, exist_ok=True)
        out_path = os.path.join(args.out, "validation_report.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"audits": audits, "all_passed": failures == 0},
                      f, ensure_ascii=False, indent=2)
        print(f"\n审计报告已写出: {out_path}")

    print(f"\n结论: {len(audits) - failures}/{len(audits)} 个样例通过")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
