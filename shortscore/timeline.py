"""时间轴运算：由小节/节拍与速度曲线共同计算真实秒数。

每段支持恒定速度或线性 BPM 斜坡（beat 0..n）：
  恒定：t(b) = b * 60 / bpm
  斜坡：t(b) = (60 / k) * ln(1 + k*b / b0)，其中 k = (b1 - b0)/n
逆运算 beat_at_time 用二分法实现，供真实音频核验时把秒定位回拍。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .model import BEATS_PER_BAR


@dataclass
class Segment:
    name: str
    beats: float
    bpm_start: float
    bpm_end: float
    start_sec: float = 0.0
    start_beat: float = 0.0

    def time_at(self, local_beat: float) -> float:
        b = min(max(local_beat, 0.0), self.beats)
        b0, b1, n = self.bpm_start, self.bpm_end, self.beats
        if n <= 0:
            return self.start_sec
        if abs(b1 - b0) < 1e-9:
            return self.start_sec + b * 60.0 / b0
        k = (b1 - b0) / n
        return self.start_sec + (60.0 / k) * math.log1p(k * b / b0)

    @property
    def duration(self) -> float:
        return self.time_at(self.beats) - self.start_sec


class TempoMap:
    """整曲速度图：段内线性 BPM、段间连续（下一段起点速度等于上一段末速可选）。"""

    def __init__(self, segments: list[Segment]):
        self.segments = segments
        t, beat = 0.0, 0.0
        for seg in segments:
            seg.start_beat = beat
            seg.start_sec = t
            t += seg.duration
            beat += seg.beats
        self.total_beats = beat
        self.total_sec = t

    def _locate(self, beat: float) -> tuple[Segment, float]:
        for seg in self.segments:
            if beat <= seg.start_beat + seg.beats + 1e-9:
                return seg, beat - seg.start_beat
        last = self.segments[-1]
        return last, last.beats

    def time_at_beat(self, beat: float) -> float:
        seg, local = self._locate(beat)
        return seg.time_at(local)

    def beat_at_time(self, t: float) -> float:
        lo, hi = 0.0, self.total_beats
        for _ in range(60):
            mid = (lo + hi) / 2
            if self.time_at_beat(mid) < t:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    def bpm_at_beat(self, beat: float) -> float:
        seg, local = self._locate(beat)
        if seg.beats <= 0:
            return seg.bpm_start
        r = local / seg.beats
        return seg.bpm_start + (seg.bpm_end - seg.bpm_start) * r

    def curve_samples(self, step_sec: float = 0.5) -> list[dict[str, float]]:
        out = []
        t = 0.0
        while t <= self.total_sec + 1e-9:
            b = self.beat_at_time(t)
            out.append({"t": round(t, 4), "beat": round(b, 4),
                        "bar": round(b / BEATS_PER_BAR, 3),
                        "bpm": round(self.bpm_at_beat(b), 3)})
            t += step_sec
        return out


def scale_segments(segments: list[Segment], scale: float) -> list[Segment]:
    """整体改速度：所有 BPM 乘 scale（scale>1 更快、时长更短）。"""
    return [Segment(s.name, s.beats, s.bpm_start * scale, s.bpm_end * scale)
            for s in segments]


def ramp_toward(segments: list[Segment], end_factor: float) -> list[Segment]:
    """让末段做渐慢/渐快收束：末段末速 = 末段起速 * end_factor。"""
    out = [Segment(s.name, s.beats, s.bpm_start, s.bpm_end) for s in segments]
    if out:
        out[-1].bpm_end = out[-1].bpm_start * end_factor
    return out
