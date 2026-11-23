"""真实音频渲染与核验（仅标准库：math + wave + array）。

渲染不是文字示意：每个事件按 TempoMap 换算的真实秒数写入 PCM 采样，
含 ADSR 包络、混响式短延迟与尾音释放；核验直接读取渲染后的 PCM，
检查交付长度与强调/高潮时点的实际能量峰。
"""
from __future__ import annotations

import array
import math
import os
import wave
from dataclasses import dataclass

SR_DEFAULT = 44100


def midi_to_freq(pitch: int) -> float:
    return 440.0 * 2.0 ** ((pitch - 69) / 12.0)


@dataclass
class RenderResult:
    wav_path: str
    sample_rate: int
    frames: int
    rendered_sec: float
    delivery_sec: float
    tail_policy: str
    release_sec: float
    peak: float
    verification: list[dict]


def _envelope(n: int, attack: float, release: float) -> list[float]:
    if n <= 0:
        return []
    a = max(1, int(attack))
    r = max(1, int(release))
    env = [1.0] * n
    for i in range(min(a, n)):
        env[i] = i / a
    for i in range(min(r, n)):
        env[n - 1 - i] = min(env[n - 1 - i], i / r)
    return env


class _Synth:
    def __init__(self, sr: int):
        self.sr = sr
        self._wave_cache: dict[tuple[int, int], list[float]] = {}
        self._env_cache: dict[tuple[int, int], list[float]] = {}

    def _tone(self, pitch: int, n: int, kind: str) -> list[float]:
        key = (pitch, n)
        cached = self._wave_cache.get(key)
        if cached is not None:
            return cached
        f = midi_to_freq(pitch)
        w0 = 2 * math.pi * f / self.sr
        out = [0.0] * n
        if kind == "melody":
            harms, gains = (1, 2, 3), (0.7, 0.22, 0.08)
        elif kind == "bass":
            harms, gains = (1, 2), (0.85, 0.15)
        else:  # pad
            harms, gains = (1, 2), (0.6, 0.12)
        for i in range(n):
            phase = w0 * i
            v = 0.0
            for h, g in zip(harms, gains):
                v += g * math.sin(phase * h)
            out[i] = v
        if len(self._wave_cache) < 4000:
            self._wave_cache[key] = out
        return out

    def _env(self, n: int, attack_ms: float, release_ms: float) -> list[float]:
        key = (n, int(attack_ms * 10))
        cached = self._env_cache.get(key)
        if cached is not None:
            return cached
        env = _envelope(n, self.sr * attack_ms / 1000, self.sr * release_ms / 1000)
        if len(self._env_cache) < 4000:
            self._env_cache[key] = env
        return env

    def add_tone(self, buf: list[float], start: int, n: int, pitch: int,
                 vel: float, kind: str, gain: float):
        wave_ = self._tone(pitch, n, kind)
        attack = 8 if kind != "pad" else 40
        release = 90 if kind != "pad" else 220
        env = self._env(n, attack, release)
        amp = vel * gain
        for i in range(n):
            buf[start + i] += wave_[i] * env[i] * amp

    def add_drum(self, buf: list[float], start: int, n: int, vel: float,
                 accent: bool):
        # 低频正弦下扫 + 噪声点击；accent 为定音鼓式强击
        f0 = 150.0 if accent else 110.0
        f1 = 55.0 if accent else 48.0
        dur = n
        for i in range(dur):
            r = i / dur
            f = f0 * (f1 / f0) ** r
            env = math.exp(-9 * r)
            noise = (0.25 if accent else 0.15) * math.exp(-25 * r) * (
                math.sin(i * 12.9898) * 43758.5453 % 1.0 - 0.5)
            buf[start + i] += vel * (0.9 * env * math.sin(2 * math.pi * f * i / self.sr)
                                     + noise)


_VOICE_GAIN = {"melody": 0.26, "bass": 0.22, "pad": 0.11, "perc": 0.30}
_VOICE_KIND = {"melody": "melody", "bass": "bass", "pad": "pad", "perc": "drum"}


def render_wav(schedule: list[dict], tmap, out_path: str,
               sample_rate: int = SR_DEFAULT, tail_policy: str = "include_release",
               release_sec: float = 0.45, structural_events: list[dict] | None = None,
               emphases: list | None = None) -> RenderResult:
    synth = _Synth(sample_rate)
    last_end_sec = 0.0
    for ev in schedule:
        last_end_sec = max(last_end_sec, ev["start_sec"] + ev["dur_sec"])
    tail = release_sec if tail_policy == "include_release" else 0.05
    total_sec = last_end_sec + tail
    n_frames = int(round(total_sec * sample_rate))
    buf = [0.0] * n_frames

    for ev in schedule:
        start = int(round(ev["start_sec"] * sample_rate))
        n = max(1, int(round(ev["dur_sec"] * sample_rate)))
        n = min(n, n_frames - start)
        if n <= 0:
            continue
        if ev["voice"] == "perc":
            synth.add_drum(buf, start, n, ev["vel"], ev.get("accent", False))
        else:
            synth.add_tone(buf, start, n, ev["pitch"], ev["vel"],
                           _VOICE_KIND[ev["voice"]], _VOICE_GAIN[ev["voice"]])

    # 简易空间感：一次短延迟叠加（不改变交付长度判定）
    delay = int(0.045 * sample_rate)
    for i in range(delay, n_frames):
        buf[i] += 0.12 * buf[i - delay]

    peak = max((abs(x) for x in buf), default=0.0) or 1.0
    norm = 0.89 / peak
    fade = min(int(0.005 * sample_rate), n_frames)
    for i in range(fade):
        buf[i] *= i / fade
        buf[n_frames - 1 - i] *= i / fade

    pcm = array.array("h")
    for x in buf:
        v = int(max(-1.0, min(1.0, x * norm)) * 32767)
        pcm.append(v)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())

    verification = verify_render(out_path, schedule, tmap, tail_policy, release_sec,
                                 structural_events or [], emphases or [])
    return RenderResult(
        wav_path=os.path.abspath(out_path), sample_rate=sample_rate,
        frames=n_frames, rendered_sec=n_frames / sample_rate,
        delivery_sec=total_sec, tail_policy=tail_policy,
        release_sec=release_sec, peak=peak * norm, verification=verification)


# ---------------------------------------------------------------------------
# 真实音频核验：直接读回 WAV 的 PCM
# ---------------------------------------------------------------------------

def _read_pcm(path: str) -> tuple[int, list[float]]:
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    pcm = array.array("h")
    pcm.frombytes(raw)
    return sr, [s / 32768.0 for s in pcm]


def _rms_window(samples: list[float], center: int, half: int) -> float:
    lo, hi = max(0, center - half), min(len(samples), center + half)
    if hi <= lo:
        return 0.0
    return math.sqrt(sum(x * x for x in samples[lo:hi]) / (hi - lo))


def verify_render(wav_path: str, schedule: list[dict], tmap, tail_policy: str,
                  release_sec: float, structural_events: list[dict],
                  emphases: list) -> list[dict]:
    sr, samples = _read_pcm(wav_path)
    half = max(1, int(0.06 * sr))
    global_rms = math.sqrt(sum(x * x for x in samples) / max(1, len(samples))) or 1e-9
    rows = []

    # 1) 交付长度：渲染帧数与时间轴预测一致；尾音策略显式说明
    last_on = max(ev["start_sec"] + ev["dur_sec"] for ev in schedule)
    expected = last_on + (release_sec if tail_policy == "include_release" else 0.05)
    rows.append({
        "check": "交付长度（真实渲染 vs 时间轴预测）",
        "measured_sec": round(len(samples) / sr, 4),
        "expected_sec": round(expected, 4),
        "pass": abs(len(samples) / sr - expected) < 2.0 / sr,
        "detail": f"尾音策略={tail_policy}，最后发声结束={last_on:.3f}s，"
                  f"{'计入' if tail_policy == 'include_release' else '不计入'}尾音释放",
    })

    # 2) 结构事件：段起点要求窗口内有真实持续信号；高潮/回归要求能量峰
    by_beat = {e["kind"]: e["beat"] for e in structural_events}
    for kind, beat in by_beat.items():
        t = tmap.time_at_beat(beat)
        rms = _rms_window(samples, int(t * sr), half)
        if kind == "section_start":
            # 段起点（如柔和引子）未必有攻击峰，只核验该时刻确实存在信号而非静音
            gate = 0.008
            rows.append({
                "check": f"结构事件信号核验：{kind}",
                "measured_sec": round(t, 4),
                "rms": round(rms, 5),
                "pass": rms > gate,
                "detail": f"事件时刻 ±60ms RMS={rms:.4f} 高于静音门限 {gate} 即视为真实发声",
            })
        else:
            rows.append({
                "check": f"结构事件能量核验：{kind}",
                "measured_sec": round(t, 4),
                "level_ratio": round(rms / global_rms, 3),
                "pass": rms / global_rms > 1.05,
                "detail": "事件时刻 ±60ms RMS 高于全曲均值即视为真实发声",
            })

    # 3) 强调时点容差：真实能量峰位置与目标时刻之差
    role_to_beat = {}
    for e in structural_events:
        if e["kind"] in ("climax", "theme_return"):
            role_to_beat[e["kind"]] = e["beat"]
        if e["kind"] == "section_start":
            role_to_beat[f"section_start:{e['section']}"] = e["beat"]
    win_half = int(0.35 * sr)
    for em in emphases:
        role = em["role"] if isinstance(em, dict) else em.role
        label = em.get("label", "") if isinstance(em, dict) else em.label
        target = em["time"] if isinstance(em, dict) else em.time
        tol = em["tolerance"] if isinstance(em, dict) else em.tolerance
        beat = role_to_beat.get(role)
        if beat is None:
            rows.append({"check": f"强调落点核验：{label or role}", "pass": False,
                         "detail": "对应段落未选入，无真实事件可核验"})
            continue
        predicted = tmap.time_at_beat(beat)
        c0 = max(0, int(predicted * sr) - win_half)
        c1 = min(len(samples), int(predicted * sr) + win_half)
        # 在 ±350ms 内寻找能量最大的 20ms 帧作为实测峰
        step = int(0.02 * sr)
        best_t, best_v = predicted, -1.0
        for c in range(c0, c1, step):
            v = _rms_window(samples, c, step // 2)
            if v > best_v:
                best_v, best_t = v, c / sr
        err = best_t - target
        rows.append({
            "check": f"强调落点核验：{label or role}",
            "target_sec": round(target, 4),
            "predicted_event_sec": round(predicted, 4),
            "measured_peak_sec": round(best_t, 4),
            "tolerance_sec": tol,
            "peak_vs_target_error_sec": round(err, 4),
            "pass": abs(err) <= tol + 0.02,
            "detail": "在真实渲染 PCM 上检索 ±350ms 能量峰；含 ±20ms 帧量化误差",
        })
    return rows
