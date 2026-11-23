# 短配乐结构生成器 shortscore

给定**自制主题材料、允许的再现/对比材料、目标时长与若干强调时点**，程序在
**引子 → 发展 → 回归 → 收束** 的受支持段落上规划真实曲式，输出完整谱面、
真实渲染音频、段落依据与可复算工程。仅依赖 Python 3.11 标准库（无需 pip 安装）。

## 图形入口（曲式规划主入口）

```bash
python -m shortscore.webapp            # 默认 http://127.0.0.1:8000
python -m shortscore.webapp --port 8012
```

浏览器打开后可：

- 拖动结构段（引子/收束可在固定顺序内交换小节配置）、用 ± 设置各段小节数与省略；
- 编辑目标时长、强调时点与容差、尾音策略、主题复现次数区间；
- 结果区呈现**由后端实际运算**得到的曲式布局、速度曲线、强调落点与完整播放；
- 一键比较「改速度图 vs 替换乐段」、导出 MusicXML 与 JSON；
- 任何约束冲突或非法输入都由界面顶部红框直接提示，并返回误差最小的候选结构。

> 命令行仅辅助曲式规划；图形界面是规划主入口。

## 命令行（辅助）

```bash
python -m shortscore.cli plan examples/1_feasible.json --outdir build/demo
python -m shortscore.cli compare examples/2_must_change_tempo.json
python -m shortscore.cli render examples/1_feasible.json build/demo/preview.wav
python build_examples.py          # 重新生成全部样例交付物
python -m unittest discover -s tests -v
```

## 核心约束（不可绕过）

1. **乐句长度不能整除目标片长**（小节口径）：若 `总小节 % 乐句小节 == 0`，
   判定为"整段循环退化"并拒绝；规划网格中所有整除组合都会被标记违规。
2. **不能靠把一段循环拉满时间**：发展段至少含 B→A 两个对比乐句，高潮固定在
   发展段最后一个乐句的强拍；零头小节由半乐句/属悬留/终止式消化，不裁音符。
3. **总时长由小节、节拍与速度曲线共同计算**：每段支持恒定或线性 BPM 斜坡，
   目标时长反解整体速度缩放；超出 `tempo_limits` 即报"必须改变结构"。
4. **主题复现次数受约束**：引子/发展/回归/收束各自的 A、B 陈述数合计须落入区间。
5. **强调时点带容差且在真实渲染音频上核验**：读取渲染后的 PCM，在事件 ±350ms
   内检索能量峰，报告实测峰值时刻与误差，而不是只比对时间轴预测。
6. **尾音是否计入交付长度显式声明**：`include_release`（发声总长+释放=目标）
   或 `exclude_release`（到最后发声为止）；两种口径都会在核验报告写明。
7. **最后一个乐句必须有材料支持的结束**：收束段末尾固定 I–V–I 正格终止，
   不允许以裁掉尾部对齐时长。

## 三个内置样例

| 样例 | 情形 | 结果 |
| --- | --- | --- |
| `examples/1_feasible.json` | 长度可行 | 17 小节（4+4+6+3，非整除），x1.05 速度，全部强调落入容差，渲染 45.00s |
| `examples/2_must_change_tempo.json` | 必须改变速度 | 名义速度换乐段最短 43.63s 且触发整除/容差违规；改速度 x1.21 后 40.00s 可行 |
| `examples/3_emphasis_conflict.json` | 无法同时满足 | 两强调节点目标间距 1.6s，但所有候选结构最小可达间距约 4s，结构性不可解 |

每个样例在 `build/<名称>/` 下产出：

- `score.musicxml`：四声部完整谱面（旋律/低音/和声垫/打击乐，含段落与速度标记）；
- `score.json`：机器可读事件谱、约束、速度曲线、核验结果；
- `preview.wav`：44.1kHz/16bit 单声道真实渲染；
- `strategy_comparison.json`：两条对齐路线对比；
- `structure_rationale.md`：段落依据、误差来源与不可行根因。

## 误差来源（工程上显式暴露）

- 速度网格量化：BPM 由闭式反解，段内斜坡用对数闭式公式，误差 < 1e-4s；
- 渲染帧量化：采样率 44100，单帧 ≈ 0.023ms；能量峰检索帧宽 20ms（±20ms）；
- 尾音口径：释放时长按策略计入/排除，差值在 `score.json.constraints` 可复算；
- 结构网格：小节数只能在 `bar_ranges` 网格上取，离散化误差通过候选排序透明展示。

字段规格与自制材料写法见 [docs/USAGE.md](docs/USAGE.md)。
