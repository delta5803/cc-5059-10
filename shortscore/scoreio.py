"""完整谱面导出：MusicXML（可在 MuseScore/Finale 打开）与 JSON 事件谱。"""
from __future__ import annotations

import json
from xml.sax.saxutils import escape

from .model import BEATS_PER_BAR, Plan

_DUR_TYPES = {1.0: "whole", 2.0: "half", 0.5: "eighth",
              1.5: "quarter", 4.0: "whole"}
_STEP = ["C", "C", "D", "D", "E", "F", "F", "G", "G", "A", "A", "B"]
_ALTER = [0, 1, 0, 1, 0, 0, 1, 0, 1, 0, 1, 0]


def _note_xml(pitch: int, dur_beats: float, voice: int, percussion: bool = False,
              accent: bool = False) -> str:
    typ = _DUR_TYPES.get(dur_beats, "quarter")
    if percussion:
        head = ("<note><accent/>" if accent else "<note>") + \
            f'<unpitched><display-step>F</display-step><display-octave>4</display-octave>' \
            f'</unpitched><duration>{int(dur_beats)}</duration><type>{typ}</type>' \
            '<instrument id="P1"/></note>'
        return head
    octave = pitch // 12 - 1
    pc = pitch % 12
    acc = ' <accidental>sharp</accidental>' if _ALTER[pc] else ''
    return (
        f"<note>{'<accent/>' if accent else ''}"
        f"<pitch><step>{_STEP[pc]}</step>"
        f"{'<alter>1</alter>' if _ALTER[pc] else ''}"
        f"<octave>{octave}</octave></pitch>{acc}"
        f"<duration>{int(dur_beats) if dur_beats == int(dur_beats) else dur_beats}</duration>"
        f"<voice>{voice}</voice><type>{typ}</type></note>"
    )


def write_musicxml(plan: Plan, out_path: str, title: str = "短配乐") -> str:
    parts = [
        ("P1", "旋律 Melody", "melody"),
        ("P2", "低音 Bass", "bass"),
        ("P3", "和声垫 Pad", "pad"),
        ("P4", "打击乐 Percussion", "perc"),
    ]
    total_bars = plan.total_bars
    # 按小节聚合事件
    bars: dict[str, list[list[dict]]] = {pid: [[] for _ in range(total_bars)]
                                         for _, _, pid in parts}
    for ev in plan.schedule:
        idx = int(ev["start_beat"] // BEATS_PER_BAR)
        if 0 <= idx < total_bars:
            bars[ev["voice"]][idx].append(ev)

    # 段落标记（每段首小节）
    section_at_bar = {}
    for s in plan.sections:
        section_at_bar[int(s.start_beat // BEATS_PER_BAR)] = s
    tempo_at_bar = {int(s.start_beat // BEATS_PER_BAR): s for s in plan.sections}

    body_parts = []
    for pid, pname, key in parts:
        p = [f'<part id="{pid}">']
        for bar_idx in range(total_bars):
            attrs = []
            if bar_idx == 0:
                attrs.append('<attributes><divisions>1</divisions><key><fifths>0</fifths></key>'
                             '<time><beats>4</beats><beat-type>4</beat-type></time>'
                             '<clef><sign>G</sign><line>2</line></clef></attributes>')
            if bar_idx in tempo_at_bar:
                sec = tempo_at_bar[bar_idx]
                attrs.append(f'<direction placement="above"><direction-type><metronome>'
                             f'<beat-unit>quarter</beat-unit><per-minute>'
                             f'{round(sec.bpm_start)}</per-minute></metronome>'
                             f'</direction-type><sound tempo="{round(sec.bpm_start, 2)}"/>'
                             f'</direction>')
            if bar_idx in section_at_bar:
                sec = section_at_bar[bar_idx]
                cn = {"intro": "引子 Intro", "development": "发展 Development",
                      "recap": "回归 Recap", "coda": "收束 Coda"}[sec.kind]
                attrs.append(f'<direction placement="above"><direction-type><rehearsal '
                             f'font-weight="bold">{escape(cn)}</rehearsal></direction-type>'
                             f'</direction>')
            note_xml = []
            seen_local_beats: set[float] = set()
            for ev in sorted(bars[key][bar_idx], key=lambda e: e["start_beat"]):
                local = round(ev["start_beat"] - bar_idx * BEATS_PER_BAR, 4)
                # pad 同拍多音写成和弦
                chord = local in seen_local_beats and key == "pad"
                seen_local_beats.add(local)
                nx = _note_xml(ev["pitch"], ev["dur_beats"], 1,
                               percussion=(key == "perc"), accent=ev.get("accent", False))
                if chord:
                    nx = nx.replace("<note>", "<note><chord/>", 1)
                note_xml.append(nx)
            # 该小节无事件则补整小节休止
            if not bars[key][bar_idx]:
                note_xml.append('<note><rest/><duration>4</duration><voice>1</voice>'
                                '<type>whole</type></note>')
            tag = "measure" if bar_idx else "measure"
            p.append(f'<{tag} number="{bar_idx + 1}">' + "".join(attrs)
                     + "".join(note_xml) + f"</{tag}>")
        p.append("</part>")
        body_parts.append("".join(p))

    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        '<score-partwise version="3.1"><work><work-title>' + escape(title) +
        '</work-title></work><part-list>' +
        "".join(f'<score-part id="{pid}"><part-name>{escape(pname)}</part-name>'
                f'<score-instrument id="{pid}-i"><instrument-name>{escape(pname)}'
                f'</instrument-name></score-instrument></score-part>'
                for pid, pname, _ in parts) +
        '</part-list>' + "".join(body_parts) + '</score-partwise>'
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(xml)
    return out_path


def write_json_score(plan: Plan, out_path: str, render: dict | None = None) -> str:
    payload = {
        "title": "短配乐结构谱",
        "feasible": plan.feasible,
        "violations": plan.violations,
        "constraints": {
            "target_sec": plan.target_sec,
            "delivery_sec": round(plan.delivery_sec, 4),
            "nominal_sec": round(plan.nominal_sec, 4),
            "tail_policy": plan.tail_policy,
            "total_bars": plan.total_bars,
            "phrase_bars": plan.phrase_bars,
            "phrase_divides_total_bars": plan.phrase_divides,
            "bpm_scale": round(plan.bpm_scale, 4),
            "theme_counts": plan.theme_counts,
        },
        "sections": [{
            "kind": s.kind, "bars": s.bars,
            "start_beat": s.start_beat, "start_sec": round(s.start_sec, 4),
            "bpm_start": round(s.bpm_start, 3), "bpm_end": round(s.bpm_end, 3),
            "variant": s.variant, "statements": s.statements,
        } for s in plan.sections],
        "emphasis_events": plan.emphasis_events,
        "tempo_curve": plan.tempo_curve,
        "schedule": plan.schedule,
        "render": render or {},
        "comparison": plan.comparison,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return out_path
