"""命令行（仅辅助曲式规划）。

图形化编曲试听界面才是曲式规划主入口，启动方式：
    python -m shortscore.webapp
详见 docs/USAGE.md。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .planner import compare_strategies, plan_structure
from .renderer import render_wav
from .scoreio import write_json_score, write_musicxml


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _print_plan(plan, tmap) -> None:
    print(f"可行性: {'通过' if plan.feasible else '不通过（返回误差最小候选）'}")
    for v in plan.violations:
        print(f"  ! {v}")
    print(f"总小节={plan.total_bars}（乐句 {plan.phrase_bars} 小节，"
          f"整除={plan.phrase_divides}）  速度缩放=x{plan.bpm_scale:.3f}")
    print(f"目标={plan.target_sec:.2f}s  名义(不含尾音)={plan.nominal_sec:.2f}s  "
          f"交付(尾音策略 {plan.tail_policy})={plan.delivery_sec:.2f}s")
    print(f"主题复现次数: {plan.theme_counts}")
    for s in plan.sections:
        print(f"  - {s.kind:<11} {s.bars} 小节  起 {s.start_sec:6.2f}s  "
              f"BPM {s.bpm_start:6.2f}->{s.bpm_end:6.2f}  {s.variant}")
    print("强调时点:")
    for e in plan.emphasis_events:
        if e["actual"] is None:
            print(f"  ! {e['role']}: 无对应段落事件")
        else:
            print(f"  - {e['role']:<22} 目标 {e['target']:6.2f}s 实际 {e['actual']:6.2f}s "
                  f"误差 {e['error']:+.2f}s 容差 ±{e['tolerance']}s "
                  f"{'OK' if e['matched'] else '超差'}")


def cmd_plan(args) -> int:
    spec = _load(args.spec)
    plan, scheduled, tmap = plan_structure(spec, strategy=args.strategy,
                                           ritardando=not args.no_ritardando)
    _print_plan(plan, tmap)
    if args.outdir:
        os.makedirs(args.outdir, exist_ok=True)
        res = render_wav(plan.schedule, tmap,
                         os.path.join(args.outdir, "preview.wav"),
                         sample_rate=spec.get("sample_rate", 44100),
                         tail_policy=spec.get("tail_policy", "include_release"),
                         release_sec=spec.get("release_sec", 0.45),
                         structural_events=scheduled.structural,
                         emphases=spec.get("emphases", []))
        write_musicxml(plan, os.path.join(args.outdir, "score.musicxml"),
                       spec.get("title", "短配乐"))
        write_json_score(plan, os.path.join(args.outdir, "score.json"),
                         render={"wav": res.wav_path, "rendered_sec": res.rendered_sec,
                                 "verification": res.verification})
        print(f"\n已写出: {args.outdir}/{{preview.wav, score.musicxml, score.json}}")
        print("真实音频核验:")
        for row in res.verification:
            print(f"  [{'PASS' if row['pass'] else 'FAIL'}] {row['check']} {row.get('detail', '')}")
    # 不可行属于规划结果而非命令失败：交付物仍已生成，feasible 字段与 violations 已显式标注
    return 0


def cmd_compare(args) -> int:
    report = compare_strategies(_load(args.spec))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_render(args) -> int:
    spec = _load(args.spec)
    plan, scheduled, tmap = plan_structure(spec)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    res = render_wav(plan.schedule, tmap, args.out,
                     sample_rate=spec.get("sample_rate", 44100),
                     tail_policy=spec.get("tail_policy", "include_release"),
                     release_sec=spec.get("release_sec", 0.45),
                     structural_events=scheduled.structural,
                     emphases=spec.get("emphases", []))
    print(f"渲染完成: {res.wav_path}  {res.rendered_sec:.3f}s  峰值 {res.peak:.3f}")
    for row in res.verification:
        print(f"  [{'PASS' if row['pass'] else 'FAIL'}] {row['check']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m shortscore.cli",
        description="短配乐结构生成器命令行（辅助规划；图形入口: python -m shortscore.webapp）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="规划曲式，可选渲染与导出")
    p_plan.add_argument("spec", help="输入规格 JSON")
    p_plan.add_argument("--strategy", choices=["tempo_map", "section_replace"],
                        default="tempo_map")
    p_plan.add_argument("--no-ritardando", action="store_true")
    p_plan.add_argument("--outdir", help="给定后同时导出 wav/musicxml/json")
    p_plan.set_defaults(func=cmd_plan)

    p_cmp = sub.add_parser("compare", help="比较改速度图与替换乐段两条路线")
    p_cmp.add_argument("spec")
    p_cmp.set_defaults(func=cmd_compare)

    p_render = sub.add_parser("render", help="规划并渲染真实音频")
    p_render.add_argument("spec")
    p_render.add_argument("out")
    p_render.set_defaults(func=cmd_render)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
