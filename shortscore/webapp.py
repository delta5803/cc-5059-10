"""图形编曲试听界面（曲式规划主入口）。

启动：python -m shortscore.webapp [--port 8000]
所有曲式布局、速度曲线、音频与核验均由后端真实运算返回；
前端只负责展示与拖拽交互，结构非法或约束冲突时由界面直接提示。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .planner import compare_strategies, plan_structure
from .renderer import render_wav
from .scoreio import write_json_score, write_musicxml

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
_PACKAGE_DIR = os.path.dirname(WEB_DIR)
_PROJECT_DIR = os.path.dirname(_PACKAGE_DIR)
_PRESET_DIR = os.path.join(_PROJECT_DIR, "examples")
_PRESETS = {
    "1_feasible": "样例1：长度可行",
    "2_must_change_tempo": "样例2：必须改变速度",
    "3_emphasis_conflict": "样例3：无法同时满足强调位置",
}


def _plan_payload(spec: dict, strategy: str, ritardando: bool) -> dict:
    plan, scheduled, tmap = plan_structure(spec, strategy, ritardando)
    with tempfile.TemporaryDirectory() as tmp:
        wav_path = os.path.join(tmp, "preview.wav")
        result = render_wav(
            plan.schedule, tmap, wav_path,
            sample_rate=spec.get("sample_rate", 44100),
            tail_policy=spec.get("tail_policy", "include_release"),
            release_sec=spec.get("release_sec", 0.45),
            structural_events=scheduled.structural,
            emphases=spec.get("emphases", []))
        with wave.open(wav_path, "rb") as wf:
            wav_bytes = wf.readframes(wf.getnframes())
            wav_header = wf.getparams()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setparams(wav_header)
        wf.writeframes(wav_bytes)
    return {
        "ok": plan.feasible,
        "violations": plan.violations,
        "plan": {
            "title": spec.get("title", ""),
            "target_sec": plan.target_sec,
            "delivery_sec": round(plan.delivery_sec, 3),
            "nominal_sec": round(plan.nominal_sec, 3),
            "tail_policy": plan.tail_policy,
            "total_bars": plan.total_bars,
            "phrase_bars": plan.phrase_bars,
            "phrase_divides": plan.phrase_divides,
            "bpm_scale": round(plan.bpm_scale, 4),
            "theme_counts": plan.theme_counts,
            "score": plan.score,
            "sections": [{
                "kind": s.kind, "bars": s.bars,
                "start_beat": s.start_beat, "start_sec": round(s.start_sec, 3),
                "end_sec": round(tmap.time_at_beat(s.start_beat + s.bars * 4), 3),
                "bpm_start": round(s.bpm_start, 2),
                "bpm_end": round(s.bpm_end, 2), "variant": s.variant,
            } for s in plan.sections],
            "emphasis_events": plan.emphasis_events,
            "tempo_curve": plan.tempo_curve,
            "candidates": plan.comparison["candidates_ranked"],
        },
        "verification": result.verification,
        "render": {"rendered_sec": round(result.rendered_sec, 3),
                   "sample_rate": result.sample_rate, "peak": round(result.peak, 3)},
        "wav_b64": __import__("base64").b64encode(buf.getvalue()).decode("ascii"),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 安静日志
        pass

    def _send(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                self._send(200, "text/html; charset=utf-8", f.read())
        elif path == "/app.js":
            with open(os.path.join(WEB_DIR, "app.js"), "rb") as f:
                self._send(200, "application/javascript; charset=utf-8", f.read())
        elif path == "/styles.css":
            with open(os.path.join(WEB_DIR, "styles.css"), "rb") as f:
                self._send(200, "text/css; charset=utf-8", f.read())
        elif path == "/api/presets":
            self._send_json({"presets": [{"id": k, "name": v,
                                          "spec": json.load(open(
                                              os.path.join(_PRESET_DIR, k + ".json"),
                                              encoding="utf-8"))}
                                         for k, v in _PRESETS.items()]})
        else:
            self._send(404, "text/plain; charset=utf-8", "not found".encode())

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if self.path == "/api/plan":
                spec = payload["spec"]
                strategy = payload.get("strategy", "tempo_map")
                ritardando = bool(payload.get("ritardando", True))
                self._send_json(_plan_payload(spec, strategy, ritardando))
            elif self.path == "/api/compare":
                self._send_json(compare_strategies(payload["spec"]))
            elif self.path == "/api/export":
                spec = payload["spec"]
                plan, scheduled, tmap = plan_structure(spec)
                tmp = tempfile.mkdtemp(prefix="shortscore_")
                xml_path = os.path.join(tmp, "score.musicxml")
                json_path = os.path.join(tmp, "score.json")
                wav_path = os.path.join(tmp, "preview.wav")
                res = render_wav(plan.schedule, tmap, wav_path,
                                 sample_rate=spec.get("sample_rate", 44100),
                                 tail_policy=spec.get("tail_policy", "include_release"),
                                 release_sec=spec.get("release_sec", 0.45),
                                 structural_events=scheduled.structural,
                                 emphases=spec.get("emphases", []))
                write_musicxml(plan, xml_path, spec.get("title", "短配乐"))
                write_json_score(plan, json_path,
                                 {"wav": wav_path, "rendered_sec": res.rendered_sec,
                                  "verification": res.verification})
                self._send_json({"ok": True, "musicxml": open(xml_path, encoding="utf-8").read(),
                                 "score_json": json.load(open(json_path, encoding="utf-8"))})
            else:
                self._send_json({"ok": False, "error": "未知接口"}, 404)
        except Exception as exc:  # 异常由曲式规划界面提示，不抛 500 裸栈
            self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 400)


def main(argv=None):
    parser = argparse.ArgumentParser(description="短配乐结构生成器 - 图形编曲试听界面")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"短配乐结构生成器界面: http://{args.host}:{args.port}  (Ctrl+C 退出)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
