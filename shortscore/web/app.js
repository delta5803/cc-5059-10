/* 短配乐结构生成器前端：只做展示与拖拽，所有运算结果来自后端 /api/*。 */
"use strict";

const SECTION_META = {
  intro:       { cn: "引子", color: "#5b7fb5", removable: true },
  development: { cn: "发展", color: "#b56576", removable: false },
  recap:       { cn: "回归", color: "#6a994e", removable: false },
  coda:        { cn: "收束", color: "#a68a51", removable: true },
};
const ROLE_OPTIONS = [
  ["section_start:intro", "引子起点"],
  ["section_start:development", "发展起点"],
  ["climax", "高潮落点（发展末乐句强拍）"],
  ["theme_return", "主题回归（回归段起点）"],
  ["section_start:coda", "收束起点"],
];
const $ = (id) => document.getElementById(id);
let state = null;       // 当前 spec
let lastResult = null;  // 最近一次运算结果
let audioEl = new Audio();
let progressTimer = null;

// ---------------------------------------------------------------------------
// spec 双向同步
// ---------------------------------------------------------------------------
function syncFormFromState() {
  $("title").value = state.title || "";
  $("target_sec").value = state.target_sec;
  $("phrase_bars").value = state.phrase_bars;
  $("tail_policy").value = state.tail_policy;
  $("release_sec").value = state.release_sec;
  $("tempo_lo").value = state.tempo_limits[0];
  $("tempo_hi").value = state.tempo_limits[1];
  $("recA_lo").value = state.recurrence.A[0];
  $("recA_hi").value = state.recurrence.A[1];
  $("recB_lo").value = state.recurrence.B[0];
  $("recB_hi").value = state.recurrence.B[1];
  $("raw_json").value = JSON.stringify(state, null, 2);
  renderSectionList();
  renderEmphTable();
}

function syncStateFromForm() {
  state.title = $("title").value;
  state.target_sec = Number($("target_sec").value);
  state.phrase_bars = Number($("phrase_bars").value);
  state.tail_policy = $("tail_policy").value;
  state.release_sec = Number($("release_sec").value);
  state.tempo_limits = [Number($("tempo_lo").value), Number($("tempo_hi").value)];
  state.recurrence = {
    A: [Number($("recA_lo").value), Number($("recA_hi").value)],
    B: [Number($("recB_lo").value), Number($("recB_hi").value)],
  };
  $("raw_json").value = JSON.stringify(state, null, 2);
}

function renderSectionList() {
  const ul = $("section_list");
  ul.innerHTML = "";
  for (const kind of ["intro", "development", "recap", "coda"]) {
    const meta = SECTION_META[kind];
    const choices = state.bar_ranges[kind];
    const cur = state._bars[kind];
    const li = document.createElement("li");
    // 全部段落可拖动：拖动用于在固定顺序内重分配小节配置（不改变段落顺序）
    li.draggable = true;
    li.dataset.kind = kind;
    li.innerHTML =
      `<span class="swatch" style="background:${meta.color}"></span>` +
      `<span class="name">${meta.cn} <small style="color:#7d8ea0">${kind}</small></span>` +
      `<button type="button" data-d="-1">−</button>` +
      `<span style="min-width:64px;text-align:center">${cur} 小节</span>` +
      `<button type="button" data-d="1">＋</button>` +
      `<small style="color:#7d8ea0">网格 [${choices.join("/")}]</small>`;
    li.querySelectorAll("button").forEach((b) =>
      b.addEventListener("click", () => changeBars(kind, Number(b.dataset.d))));
    li.addEventListener("dragstart", (e) => {
      li.classList.add("dragging");
      e.dataTransfer.setData("text/plain", kind);
      e.dataTransfer.effectAllowed = "move";
    });
    li.addEventListener("dragend", () => li.classList.remove("dragging"));
    li.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
    });
    li.addEventListener("drop", (e) => {
      e.preventDefault();
      const dragged = e.dataTransfer.getData("text/plain");
      if (dragged && dragged !== kind) dragSectionBars(dragged, kind);
    });
    ul.appendChild(li);
  }
}

function changeBars(kind, delta) {
  const choices = state.bar_ranges[kind];
  const cur = state._bars[kind];
  const idx = choices.indexOf(cur);
  const next = choices[Math.min(choices.length - 1, Math.max(0, idx + delta))];
  if (next === cur) return;
  state._bars[kind] = next;
  syncFormFromState();
  runPlan();
}

function dragSectionBars(from, to) {
  // 拖动结构段：在保持引子→发展→回归→收束固定顺序的前提下，
  // 把源段的小节配置移动到目标段（目标原配置顺延回源段），可省略段允许落到 0。
  const removable = (k) => SECTION_META[k].removable;
  const srcChoices = state.bar_ranges[from];
  const dstChoices = state.bar_ranges[to];
  const srcVal = state._bars[from];
  const dstVal = state._bars[to];
  const fit = (v, choices) => (choices.includes(v) ? v : choices[choices.length - 1]);
  state._bars[to] = fit(srcVal, dstChoices);
  // 不可省略段（发展/回归）不允许被拖成 0
  state._bars[from] = removable(from) ? fit(dstVal, srcChoices)
                                      : Math.max(...srcChoices.filter((v) => v > 0));
  syncFormFromState();
  runPlan();
}

function renderEmphTable() {
  const tb = $("emph_table").querySelector("tbody");
  tb.innerHTML = "";
  state.emphases.forEach((e, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td><input value="${e.label ?? ""}" data-f="label"></td>` +
      `<td><select data-f="role">${ROLE_OPTIONS.map(([v, cn]) =>
        `<option value="${v}" ${v === e.role ? "selected" : ""}>${cn}</option>`).join("")}
       </select></td>` +
      `<td><input type="number" step="0.1" value="${e.time}" data-f="time"></td>` +
      `<td><input type="number" step="0.1" value="${e.tolerance}" data-f="tolerance"></td>` +
      `<td><button type="button" data-del="${i}">删</button></td>`;
    tr.querySelectorAll("[data-f]").forEach((el) =>
      el.addEventListener("change", () => {
        const f = el.dataset.f;
        e[f] = f === "role" || f === "label" ? el.value : Number(el.value);
        $("raw_json").value = JSON.stringify(state, null, 2);
      }));
    tr.querySelector("[data-del]").addEventListener("click", () => {
      state.emphases.splice(i, 1);
      renderEmphTable();
    });
    tb.appendChild(tr);
  });
}

// 把界面 _bars（拖拽编辑结果）落回 bar_ranges，并计算允许段落
function specForRequest() {
  syncStateFromForm();
  const spec = JSON.parse(JSON.stringify(state));
  spec.bar_ranges = {};
  spec.allowed_sections = [];
  for (const kind of ["intro", "development", "recap", "coda"]) {
    const cur = state._bars[kind];
    spec.bar_ranges[kind] = [...new Set([cur, ...state.bar_ranges[kind]])].sort((a, b) => a - b);
    if (cur > 0 || !SECTION_META[kind].removable) spec.allowed_sections.push(kind);
  }
  delete spec._bars;
  return spec;
}

// ---------------------------------------------------------------------------
// 后端调用
// ---------------------------------------------------------------------------
async function post(path, body) {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return resp.json();
}

async function runPlan() {
  clearErrors();
  let payload;
  try {
    payload = await post("/api/plan", {
      spec: specForRequest(),
      strategy: $("strategy").value,
      ritardando: $("ritardando").checked,
    });
  } catch (err) {
    return showErrors([`请求失败：${err}`]);
  }
  if (payload.ok === false && payload.error) return showErrors([payload.error]);
  lastResult = payload;
  renderResult(payload);
  if (!payload.ok) showErrors(payload.violations);
}

function clearErrors() {
  const box = $("errors");
  box.hidden = true;
  box.innerHTML = "";
}
function showErrors(messages) {
  const box = $("errors");
  box.hidden = false;
  box.innerHTML = "<b>曲式规划提示：</b><ul>" +
    messages.map((m) => `<li>${m}</li>`).join("") + "</ul>" +
    "<small>已返回误差最小的候选结构；请调整段落网格、容差或速度范围后重新运算。</small>";
}

// ---------------------------------------------------------------------------
// 结果渲染
// ---------------------------------------------------------------------------
function renderResult(r) {
  const p = r.plan;
  $("metrics").innerHTML = [
    ["可行性", r.ok ? '<span class="ok">通过</span>' : '<span class="bad">不通过</span>'],
    ["目标/交付(秒)", `${p.target_sec} / ${p.delivery_sec}`],
    ["名义时长(不含尾音)", p.nominal_sec],
    ["总小节", `${p.total_bars}（乐句 ${p.phrase_bars}，整除=${p.phrase_divides}）`],
    ["速度缩放", `x${p.bpm_scale}`],
    ["主题复现", `A=${p.theme_counts.A} B=${p.theme_counts.B}`],
    ["真实渲染(秒)", r.render.rendered_sec],
  ].map(([k, v]) => `<div>${k}<b>${v}</b></div>`).join("");

  const total = p.delivery_sec;
  const tl = $("timeline");
  tl.innerHTML = "";
  p.sections.forEach((s) => {
    const div = document.createElement("div");
    div.className = "block";
    div.style.left = `${(s.start_sec / total) * 100}%`;
    div.style.width = `${((s.end_sec - s.start_sec) / total) * 100}%`;
    div.style.background = SECTION_META[s.kind].color;
    div.innerHTML = `<div class="bars">${SECTION_META[s.kind].cn} ${s.bars}小节</div>` +
      `<div>${s.start_sec.toFixed(2)}–${s.end_sec.toFixed(2)}s<br>` +
      `BPM ${s.bpm_start}→${s.bpm_end}${s.variant ? "<br>" + s.variant : ""}</div>`;
    tl.appendChild(div);
  });

  const layer = $("emphasis_layer");
  layer.innerHTML = "";
  p.emphasis_events.forEach((e) => {
    if (e.actual == null) return;
    const mk = document.createElement("span");
    mk.className = "emph-mark " + (e.matched ? "ok" : "bad");
    mk.style.left = `${(e.actual / total) * 100}%`;
    mk.textContent = `${e.label || e.role} ${e.actual.toFixed(1)}s (Δ${e.error >= 0 ? "+" : ""}${e.error})`;
    layer.appendChild(mk);
  });

  $("legend").innerHTML = Object.entries(SECTION_META).map(([k, m]) =>
    `<span><i style="background:${m.color}"></i>${m.cn}</span>`).join("") +
    `<span>尾音策略：${p.tail_policy}</span>`;

  drawTempo(p.tempo_curve);
  renderVerification(r.verification);
  renderCandidates(p.candidates);
  renderConstraints(r.constraints);

  // 音频
  audioEl.pause();
  audioEl.src = "data:audio/wav;base64," + r.wav_b64;
  $("playhead").style.display = "none";
}

function renderConstraints(c) {
  if (!c) return;
  const rows = Object.entries(c.rule_hits).map(([rule, n]) =>
    `<tr><td>${rule}</td><td>${n}</td></tr>`).join("") ||
    '<tr><td colspan="2">无规则命中（全部候选通过）</td></tr>';
  $("rule_hits").querySelector("tbody").innerHTML = rows;
  const conflict = (c.minimal_conflict_sets || []).map((s) =>
    `<li>${s.rules.join(" + ")}：${s.rule_names.join("、")}</li>`).join("");
  $("conflict_box").hidden = !c.minimal_conflict_sets?.length;
  $("conflict_out").innerHTML = conflict || "无";
  $("trace_meta").textContent = JSON.stringify({
    candidate_count: c.candidate_count,
    feasible_candidate_count: c.feasible_candidate_count,
    rule_violations: c.rule_violations,
  }, null, 2);
}

function drawTempo(curve) {
  const svg = $("tempo_svg");
  svg.innerHTML = "";
  if (!curve.length) return;
  const W = 720, H = 160, pad = 24;
  const tmax = curve[curve.length - 1].t;
  const bpms = curve.map((c) => c.bpm);
  const bmin = Math.min(...bpms), bmax = Math.max(...bpms);
  const x = (t) => pad + (t / tmax) * (W - 2 * pad);
  const y = (b) => H - pad - ((b - bmin) / Math.max(1e-6, bmax - bmin)) * (H - 2 * pad);
  const path = curve.map((c, i) => `${i ? "L" : "M"}${x(c.t).toFixed(1)},${y(c.bpm).toFixed(1)}`).join(" ");
  svg.innerHTML =
    `<line x1="${pad}" y1="${H - pad}" x2="${W - pad}" y2="${H - pad}" stroke="#33404f"/>` +
    `<line x1="${pad}" y1="${pad}" x2="${pad}" y2="${H - pad}" stroke="#33404f"/>` +
    `<text x="${pad}" y="14" fill="#8fa0b3" font-size="11">BPM ${bmax.toFixed(1)}</text>` +
    `<text x="${pad}" y="${H - 6}" fill="#8fa0b3" font-size="11">${bmin.toFixed(1)}</text>` +
    `<path d="${path}" fill="none" stroke="#7db4ff" stroke-width="2"/>`;
}

function renderVerification(rows) {
  const tb = $("verify_table").querySelector("tbody");
  tb.innerHTML = rows.map((r) => {
    const detail = Object.entries(r)
      .filter(([k]) => !["check", "pass", "detail"].includes(k))
      .map(([k, v]) => `${k}=${v}`).join(" · ");
    return `<tr><td class="${r.pass ? "ok" : "bad"}">${r.pass ? "PASS" : "FAIL"}</td>` +
      `<td>${r.check}</td><td><small>${detail} ${r.detail || ""}</small></td></tr>`;
  }).join("");
}

function renderCandidates(rows) {
  const tb = $("cand_table").querySelector("tbody");
  tb.innerHTML = rows.map((c) =>
    `<tr><td class="${c.feasible ? "ok" : "bad"}">${c.feasible ? "可行" : "受限"}</td>` +
    `<td>${JSON.stringify(c.bars)}</td><td>${c.total_bars} 小节</td>` +
    `<td>x${c.scale}</td><td>${c.delivery_sec}s</td>` +
    `<td>最大强调误差 ${c.max_emphasis_error_sec}s</td>` +
    `<td><small>${(c.violations || []).join("；")}</small></td></tr>`).join("");
}

// ---------------------------------------------------------------------------
// 播放
// ---------------------------------------------------------------------------
$("btn_play").addEventListener("click", () => {
  if (!lastResult) return;
  audioEl.currentTime = 0;
  audioEl.play();
  $("playhead").style.display = "block";
  clearInterval(progressTimer);
  progressTimer = setInterval(() => {
    const total = lastResult.plan.delivery_sec;
    const t = audioEl.currentTime;
    $("playhead").style.left = `${(t / total) * 100}%`;
    $("clock").textContent = `${t.toFixed(2)} / ${total.toFixed(2)} s`;
    if (audioEl.paused || t >= audioEl.duration) {
      clearInterval(progressTimer);
      $("playhead").style.display = "none";
    }
  }, 80);
});
$("btn_stop").addEventListener("click", () => {
  audioEl.pause();
  audioEl.currentTime = 0;
  clearInterval(progressTimer);
  $("playhead").style.display = "none";
});

// ---------------------------------------------------------------------------
// 对比与导出
// ---------------------------------------------------------------------------
$("btn_compare").addEventListener("click", async () => {
  const r = await post("/api/compare", { spec: specForRequest() });
  $("compare_box").hidden = false;
  $("compare_out").textContent = JSON.stringify(r, null, 2);
});

$("btn_export").addEventListener("click", async () => {
  const r = await post("/api/export", { spec: specForRequest() });
  if (r.ok === false) return showErrors([r.error]);
  $("export_box").hidden = false;
  const blob = new Blob([r.musicxml], { type: "application/vnd.recordare.musicxml+xml" });
  $("dl_xml").href = URL.createObjectURL(blob);
  $("export_meta").textContent = JSON.stringify({
    feasible: r.score_json.feasible,
    constraints: r.score_json.constraints,
    verification: r.score_json.render.verification,
  }, null, 2);
});

$("add_emph").addEventListener("click", () => {
  state.emphases.push({ time: 1, role: "climax", tolerance: 0.8, label: "新节点" });
  renderEmphTable();
});
$("apply_json").addEventListener("click", () => {
  try {
    const parsed = JSON.parse($("raw_json").value);
    initState(parsed);
    syncFormFromState();
    clearErrors();
  } catch (err) {
    showErrors([`JSON 解析失败：${err}`]);
  }
});
["target_sec", "phrase_bars", "tail_policy", "release_sec", "tempo_lo", "tempo_hi",
 "recA_lo", "recA_hi", "recB_lo", "recB_hi", "title", "strategy", "ritardando"]
  .forEach((id) => $(id).addEventListener("change", () => {
    syncStateFromForm();
  }));
$("btn_plan").addEventListener("click", runPlan);

// ---------------------------------------------------------------------------
// 初始化
// ---------------------------------------------------------------------------
function initState(spec) {
  state = JSON.parse(JSON.stringify(spec));
  state._bars = {};
  for (const kind of ["intro", "development", "recap", "coda"]) {
    const choices = state.bar_ranges[kind];
    const allowed = state.allowed_sections || ["intro", "development", "recap", "coda"];
    state._bars[kind] = allowed.includes(kind) ? Math.max(...choices.filter((v) => v > 0)) : 0;
    if (!choices.includes(0) && !SECTION_META[kind].removable) state._bars[kind] = choices[0];
  }
}

(async function boot() {
  const presets = await (await fetch("/api/presets")).json();
  const sel = $("preset");
  presets.presets.forEach((p) => {
    const o = document.createElement("option");
    o.value = p.id;
    o.textContent = p.name;
    sel.appendChild(o);
  });
  sel.addEventListener("change", () => {
    const preset = presets.presets.find((p) => p.id === sel.value);
    initState(preset.spec);
    syncFormFromState();
    runPlan();
  });
  initState(presets.presets[0].spec);
  syncFormFromState();
  runPlan();
})();
