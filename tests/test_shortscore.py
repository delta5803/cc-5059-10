"""shortscore 单元测试：python -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shortscore.model import BEATS_PER_BAR
from shortscore.planner import (_dev_plan_shape, _theme_counts,
                                compare_strategies, plan_structure)
from shortscore.constraints import (RULE_NAMES, check_candidate, check_spec,
                                    diagnose, minimal_conflict_sets)
from shortscore.renderer import render_wav
from shortscore.timeline import Segment, TempoMap
from shortscore.scoreio import write_musicxml, write_json_score

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX = os.path.join(ROOT, "examples")


def load(name):
    with open(os.path.join(EX, name), encoding="utf-8") as f:
        return json.load(f)


class TimelineTests(unittest.TestCase):
    def test_constant_tempo_duration(self):
        seg = Segment("x", 16, 120, 120)
        tmap = TempoMap([seg])
        self.assertAlmostEqual(tmap.total_sec, 16 * 60 / 120, places=5)

    def test_ramp_durates_longer_when_slowing(self):
        flat = TempoMap([Segment("x", 16, 100, 100)]).total_sec
        ramp = TempoMap([Segment("x", 16, 100, 80)]).total_sec
        self.assertGreater(ramp, flat)

    def test_beat_time_roundtrip(self):
        tmap = TempoMap([Segment("a", 8, 90, 120), Segment("b", 8, 120, 70)])
        for b in (0.0, 3.5, 8.0, 12.25, 16.0):
            t = tmap.time_at_beat(b)
            self.assertAlmostEqual(tmap.beat_at_time(t), b, places=4)


class ConstraintTests(unittest.TestCase):
    def test_phrase_may_not_divide_total_bars(self):
        spec = load("1_feasible.json")
        plan, _, _ = plan_structure(spec)
        self.assertFalse(plan.phrase_divides)
        self.assertNotEqual(plan.total_bars % plan.phrase_bars, 0)

    def test_feasible_example_passes_all_tolerances(self):
        plan, _, _ = plan_structure(load("1_feasible.json"))
        self.assertTrue(plan.feasible, plan.violations)
        self.assertTrue(all(e["matched"] for e in plan.emphasis_events))

    def test_section_replace_cannot_hit_example2_target(self):
        plan, _, _ = plan_structure(load("2_must_change_tempo.json"),
                                    "section_replace", ritardando=False)
        self.assertFalse(plan.feasible)
        self.assertTrue(any("替换乐段" in v or "速度缩放" in v or "偏离目标" in v
                            for v in plan.violations))

    def test_tempo_strategy_resolves_example2(self):
        plan, _, _ = plan_structure(load("2_must_change_tempo.json"))
        self.assertTrue(plan.feasible, plan.violations)
        self.assertNotAlmostEqual(plan.bpm_scale, 1.0, places=2)

    def test_emphasis_conflict_is_unresolvable(self):
        plan, _, _ = plan_structure(load("3_emphasis_conflict.json"))
        self.assertFalse(plan.feasible)
        self.assertTrue(any("卡点" in v for v in plan.violations))

    def test_no_single_loop_filling_time(self):
        # 任何可行解都必须包含发展与回归两个实质不同的段落
        plan, _, _ = plan_structure(load("1_feasible.json"))
        kinds = [s.kind for s in plan.sections]
        self.assertIn("development", kinds)
        self.assertIn("recap", kinds)
        self.assertGreaterEqual(len(plan.schedule), 30)

    def test_tail_policy_changes_delivery_length_explicitly(self):
        raw = load("1_feasible.json")
        p1, _, _ = plan_structure(raw)
        raw2 = dict(raw, tail_policy="exclude_release")
        p2, _, _ = plan_structure(raw2)
        # 两种策略交付时长都回到目标；区别体现在是否显式计入释放与所用速度
        self.assertEqual(p1.tail_policy, "include_release")
        self.assertEqual(p2.tail_policy, "exclude_release")
        self.assertAlmostEqual(p1.delivery_sec, raw["target_sec"], delta=0.25)
        self.assertAlmostEqual(p2.delivery_sec, raw["target_sec"], delta=0.25)
        self.assertNotEqual(round(p1.bpm_scale, 4), round(p2.bpm_scale, 4))

    def test_compare_strategies_outputs_both_routes(self):
        rep = compare_strategies(load("2_must_change_tempo.json"))
        self.assertIn("tempo_map", rep)
        self.assertIn("section_replace", rep)
        self.assertEqual(len(rep["summary"]), 3)

    def test_development_shape_always_two_phrases_with_climax_not_at_start(self):
        for bars in (4, 6, 8):
            shape = _dev_plan_shape(bars, 4)
            self.assertGreaterEqual(len(shape), 2)
            self.assertEqual(sum(pb for _, pb in shape), bars)


class RendererTests(unittest.TestCase):
    def test_rendered_wav_matches_predicted_length_and_events_are_audible(self):
        raw = load("1_feasible.json")
        plan, scheduled, tmap = plan_structure(raw)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.wav")
            res = render_wav(plan.schedule, tmap, path,
                             tail_policy=raw["tail_policy"],
                             release_sec=raw["release_sec"],
                             structural_events=scheduled.structural,
                             emphases=raw["emphases"])
            with wave.open(path, "rb") as wf:
                self.assertEqual(wf.getframerate(), 44100)
                self.assertEqual(wf.getnchannels(), 1)
                frames = wf.getnframes()
            self.assertAlmostEqual(frames / 44100, res.delivery_sec, places=3)
            checks = {r["check"]: r["pass"] for r in res.verification}
            self.assertTrue(checks["交付长度（真实渲染 vs 时间轴预测）"])
            self.assertTrue(checks["结构事件能量核验：climax"])
            self.assertTrue(checks["结构事件能量核验：theme_return"])
            for e in plan.emphasis_events:
                key = next(k for k in checks if (e.get("label") or e["role"]) in k)
                self.assertTrue(checks[key], key)

    def test_cadence_ends_on_tonic_not_truncated(self):
        plan, _, _ = plan_structure(load("1_feasible.json"))
        coda = next(s for s in plan.sections if s.kind == "coda")
        last_beat = coda.start_beat + coda.bars * BEATS_PER_BAR
        tail = [e for e in plan.schedule if e["start_beat"] >= last_beat - BEATS_PER_BAR]
        # 最后一小节有旋律主音收束且事件持续到小节终点，而非被裁断
        melody = [e for e in tail if e["voice"] == "melody"]
        self.assertTrue(melody)
        end = max(e["start_beat"] + e["dur_beats"] for e in tail)
        self.assertAlmostEqual(end, last_beat, places=3)


class ExportTests(unittest.TestCase):
    def test_musicxml_and_json_export(self):
        plan, _, _ = plan_structure(load("1_feasible.json"))
        with tempfile.TemporaryDirectory() as tmp:
            xmlp = os.path.join(tmp, "s.musicxml")
            jsonp = os.path.join(tmp, "s.json")
            write_musicxml(plan, xmlp)
            write_json_score(plan, jsonp)
            with open(xmlp, encoding="utf-8") as f:
                xml = f.read()
            self.assertIn("score-partwise", xml)
            self.assertEqual(xml.count("<part "), 4)
            with open(jsonp, encoding="utf-8") as f:
                payload = json.load(f)
            self.assertEqual(payload["constraints"]["total_bars"], plan.total_bars)
            self.assertGreater(len(payload["schedule"]), 30)


class ConstraintModuleTests(unittest.TestCase):
    def test_rule_names_cover_seven_documented_rules(self):
        self.assertEqual(set(RULE_NAMES), {f"R{i}" for i in range(1, 8)})

    def test_check_spec_agrees_with_planner_on_all_examples(self):
        # 约束模块与 planner 的可行性结论必须一致（不允许伪报/瞒报）
        for name in ("1_feasible.json", "2_must_change_tempo.json",
                     "3_emphasis_conflict.json"):
            raw = load(name)
            report = check_spec(raw)
            plan, _, _ = plan_structure(raw)
            self.assertEqual(report.feasible, plan.feasible, name)
            self.assertGreater(report.candidate_count, 0)

    def test_planning_trace_covers_every_candidate_with_rule_evidence(self):
        report = check_spec(load("1_feasible.json"))
        self.assertEqual(len(report.trace), report.candidate_count)
        for row in report.trace:
            self.assertIn("bars", row)
            self.assertIn("violated_rules", row)
            self.assertIn("theme_counts_actual", row)
            self.assertEqual(row["feasible"], not row["violations"])

    def test_theme_counts_match_scheduler_phrases_including_remainder(self):
        # 6 小节 = 1 整乐句 + 1 零头半乐句 = 2 个 A 陈述（旧实现漏数零头）
        counts = _theme_counts({"intro": 0, "development": 4,
                                "recap": 6, "coda": 0}, 4)
        self.assertEqual(counts["A"], 3)   # 发展段 1 个 A + 回归 2 个 A
        self.assertEqual(counts["B"], 1)
        # 收束 2 小节纯 I-V-I 终止式，不计主题陈述
        counts_coda = _theme_counts({"intro": 0, "development": 4,
                                     "recap": 4, "coda": 2}, 4)
        self.assertEqual(counts_coda["A"], 2)
        # 零发展段不产生虚假乐句
        self.assertEqual(_theme_counts({"development": 0}, 4),
                         {"A": 0, "B": 0})

    def test_development_always_contains_both_themes(self):
        for bars in (4, 6, 8):
            themes = {t for t, _ in _dev_plan_shape(bars, 4)}
            self.assertEqual(themes, {"A", "B"})

    def test_minimal_conflict_set_relax_then_restore(self):
        raw = load("3_emphasis_conflict.json")
        self.assertFalse(check_spec(raw).feasible)
        conflicts = minimal_conflict_sets(raw)
        self.assertTrue(conflicts)
        # 根因是强调规则（R5）单点冲突
        self.assertIn(["R5"], [c["rules"] for c in conflicts])
        diag = diagnose(raw)
        for chk in diag["restore_check"]["checks"]:
            self.assertTrue(chk["feasible_when_relaxed"])
            self.assertFalse(chk["feasible_after_restore"])
            self.assertTrue(chk["restore_matches_baseline"])

    def test_feasible_spec_has_empty_conflict_sets(self):
        diag = diagnose(load("1_feasible.json"))
        self.assertTrue(diag["feasible"])
        self.assertEqual(diag["minimal_conflict_sets"], [])

    def test_strict_duration_and_locked_climax_checked_per_candidate(self):
        raw = load("1_feasible.json")
        report = check_spec(raw)
        feasible_trace = next(t for t in report.trace if t["feasible"])
        self.assertNotIn("R3", feasible_trace["violated_rules"])
        # R2 锁定高潮：可行候选不得命中 R2
        self.assertNotIn("R2", feasible_trace["violated_rules"])

    def test_material_check_rejects_phrase_overflow(self):
        from shortscore.model import load_spec
        from shortscore.constraints import check_materials
        raw = load("1_feasible.json")
        spec = load_spec(raw)
        spec["materials"]["A"].melody[0].start = 999  # 超出 4 小节乐句边界
        violations = check_materials(spec)
        self.assertTrue(any(v.rule == "R2" for v in violations))


class RendererCoverageTests(unittest.TestCase):
    def test_every_section_start_is_independently_verified(self):
        # 回归：旧实现用字典按 kind 聚合，多个 section_start 互相覆盖
        raw = load("1_feasible.json")
        plan, scheduled, tmap = plan_structure(raw)
        n_sections = len({e["section"] for e in scheduled.structural
                          if e["kind"] == "section_start"})
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.wav")
            res = render_wav(plan.schedule, tmap, path,
                             tail_policy=raw["tail_policy"],
                             release_sec=raw["release_sec"],
                             structural_events=scheduled.structural,
                             emphases=raw["emphases"])
        section_rows = [r for r in res.verification
                        if r["check"].startswith("结构事件信号核验")]
        self.assertEqual(len(section_rows), n_sections)
        self.assertTrue(all(r["pass"] for r in section_rows))


if __name__ == "__main__":
    unittest.main()
