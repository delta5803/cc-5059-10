# 使用手册

## 1. 图形入口（推荐）

```bash
python -m shortscore.webapp [--port 8000] [--host 127.0.0.1]
```

打开 `http://127.0.0.1:8000`：

1. 顶部下拉选择三个内置样例，或直接在「原始规格 JSON」粘贴自定义规格后点「应用 JSON」。
2. 左侧编辑目标时长、乐句长度、尾音策略、速度缩放区间、段落小节数、强调时点、复现约束。
   - 段落卡片可拖动（仅引子/收束可交换配置；发展/回归顺序固定且不可省略）。
   - `±` 按钮在该段 `bar_ranges` 网格内切换小节数，`0` 表示省略引子或收束。
3. 点「重新运算曲式」。右侧结果区全部来自后端真实运算：
   - 曲式时间轴（按实际秒数比例绘制）与强调标记（绿=容差内，红=超差）；
   - 速度曲线 SVG、完整音频播放与播放头；
   - 真实 PCM 核验表、候选结构排序表；
   - 违规时顶部红框提示，仍给出误差最小候选。
4. 「比较：改速度 vs 换乐段」输出两条路线的结构、速度范围、复现次数与误差。
5. 「导出 MusicXML/JSON」下载完整谱面与机器可读结果。

## 2. 规格 JSON 字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `title` | string | 标题 |
| `target_sec` | number | 目标交付时长（秒） |
| `phrase_bars` | int | 乐句长度（小节）；约束要求总小节不能被它整除 |
| `tail_policy` | `include_release` / `exclude_release` | 尾音释放是否计入交付长度 |
| `release_sec` | number | 尾音释放秒数（默认 0.45） |
| `tempo_limits` | [lo, hi] | 允许的整体速度缩放区间（相对名义 BPM） |
| `tempo_bpm` | object | 各段名义速度，如 `{"intro":72,"development":104,...}` |
| `bar_ranges` | object | 各段允许小节数网格，如 `{"development":[4,6,8]}` |
| `allowed_sections` | string[] | 允许段落，子集 of `intro/development/recap/coda` |
| `emphases` | array | 强调时点，见下 |
| `recurrence` | object | 主题复现次数区间，如 `{"A":[2,4],"B":[1,2]}` |
| `materials` | object | 自制主题（可选，缺省用内置 A/B） |
| `sample_rate` | int | 渲染采样率，默认 44100 |
| `seed` | int | 预留随机种子 |

### emphases 元素

```json
{"time": 17.0, "role": "climax", "tolerance": 0.9, "label": "产品高光"}
```

`role` 必须绑定到真实音乐事件：

- `section_start:intro` / `section_start:development` / `section_start:recap` / `section_start:coda`
- `climax`：发展段最后一个乐句的强拍（位置由结构决定，不能任意指定）
- `theme_return`：回归段起点（主题 A 再现）

绑定段落未选入时，该节点直接判违规并在界面提示。

## 3. 自制材料

每个主题是 4 小节（16 拍）的四声部素材，音符用 MIDI 音高（60=中央 C，`null` 为休止）：

```json
"materials": {
  "A": {
    "name": "我的主题",
    "phrase_bars": 4,
    "melody": [
      {"pitch": 72, "start": 0, "dur": 1, "vel": 0.8},
      {"pitch": 76, "start": 1, "dur": 1, "vel": 0.8}
    ],
    "bass": [
      {"pitch": 48, "start": 0, "dur": 2, "vel": 0.7}
    ],
    "chords": [[0, "C"], [4, "G"], [8, "F"], [12, "C"]],
    "percussion": [
      {"pitch": 0, "start": 0, "dur": 1, "vel": 0.5}
    ]
  }
}
```

- `chords` 支持 `C Cm C7 Cmaj7 Cm7 Cdim Cdim7` 及升降号（如 `G#m`、`Eb7`）；
- 和声垫由和弦标签自动排布，低音取和弦根音；
- 打击乐 `pitch` 固定写 0，重音事件（高潮/终止）由调度器自动生成 `pitch=1`。

发展段变奏规则：`repeat`（原样）、`octave_up`（高八度）、`sequence_up/down`
（上/下二度模进）、`soft`（弱奏，用于引子与收束）。

## 4. 规划与核验如何运算

1. 在 `bar_ranges` 笛卡尔网格上枚举所有真实段落组合（发展/回归不可省略）。
2. 对每个组合按名义 BPM 建 `TempoMap`，由目标交付时长闭式反解速度缩放
   （渐慢变体先做末段 BPM 斜坡再精确校正，保证交付时长零漂移）。
3. 把强调 role 映射到绝对拍事件，换算真实秒数并计算容差误差与顺序冲突。
4. 校验复现次数区间、乐句非整除、速度缩放区间；全部通过才 `feasible=true`。
5. 调度器把段落展开为四声部绝对拍事件，收束段末尾写 I–V–I 终止。
6. 渲染器把事件写入 PCM（加性合成 + ADSR + 短延迟），再读回 WAV 核验：
   - 渲染帧数 vs 时间轴预测长度；
   - 高潮/回归时刻 RMS 能量峰、段起点信号门限；
   - 强调目标时刻 ±350ms 内实测能量峰位置与误差。

## 5. 命令行（辅助规划）

```bash
python -m shortscore.cli plan <spec.json> [--strategy tempo_map|section_replace]
                                          [--no-ritardando] [--outdir DIR]
python -m shortscore.cli compare <spec.json>
python -m shortscore.cli render <spec.json> <out.wav>
python build_examples.py
python -m unittest discover -s tests -v
```

不可行不是命令失败：仍会生成误差最小候选的音频与谱面，`feasible` 与
`violations` 在 JSON、界面和 Markdown 依据中显式标注。
