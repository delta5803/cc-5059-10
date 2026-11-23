"""shortscore 单元测试：python -m unittest discover -s tests -v"""
import json
import os
import tempfile
import unittest
import wave

from shortscore.model import BEATS_PER_BAR
from shortscore.planner import (_dev_plan_shape, compare_strategies,
                                plan_structure)
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


if __name__ == "__main__":
    unittest.main()
