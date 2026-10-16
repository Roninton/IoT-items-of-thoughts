# drawvid —— 一句话成片（纯标准库参考实现）

给一句话，出一支能播的片子。**全程不用任何生成式模型，不用外部进程，不用第三方库。**

参考形态来自「代码动效视频」那条路线：核心不是"生成画面"，而是**画面是时间的确定性函数**。
模型写出代码，代码逐帧算出画面，因此文字永远清晰、数据永远准确、任何一帧都能精确修改，
而且**改一个细节，其余保持不变**。

## 唯一事实来源：时间轴

```
Timeline（唯一事实来源）
  ├─► 画面：render_frame(timeline, t) ──► 帧序列 PNG + movie.apng
  ├─► 字幕：write_vtt(timeline)       ──► subs.vtt
  ├─► 音乐：synthesize_bgm(timeline)  ──► bgm.wav
  ├─► 清单：timeline.to_json()        ──► plan.json
  └─► 播放：write_player(timeline)    ──► index.html
```

**多路输出必须互相对齐**，这是本项目最重要的一条可测性质：帧数等于 `frame_count()`；
字幕落在时间轴时长内且首尾相接；BGM 采样数等于 `duration_ms * 22050 // 1000`；
清单里的时长与帧数一致。

## 快速开始

```python
from drawvid.pipeline import make_video

apng = make_video("「演示」把素材做成 6 秒的片子，节奏放慢。", "assets", "out")
print(apng)  # out/movie.apng
```

也可以直接跑端到端演示：

```bash
python demo.py
```

它会自己造两张 PNG、规划时间轴、渲染全部帧，并把六项产物写到 `out/demo/`：

| 产物 | 内容 |
|---|---|
| `plan.json` | `timeline.to_json()` |
| `frames/frame_%04d.png` | 逐帧 PNG，数量 = `frame_count()` |
| `movie.apng` | 上述帧封装的**真 APNG**（acTL / fcTL / IDAT / fdAT） |
| `subs.vtt` | WebVTT 字幕（旁挂，不烧进画面） |
| `bgm.wav` | 代码合成的器乐 BGM（单声道 / 16 位 / 22050Hz） |
| `index.html` | 播放页：逐帧画面 + `<track>` 字幕 + `<audio>` |

## 确定性

`render_frame(timeline, t_ms, images)` 是**纯函数**：同样的入参 → 逐像素完全相同的输出，
跨进程、跨次运行都成立。实现里不读时钟、不用随机、不读环境变量、不依赖当前目录、
不留跨调用状态、不做任何 I/O。

理由很实际：确定性才能让**任意一帧单独重算**、让冻结测试客观判定、
让"预览与导出逐帧一致"。一旦引入随机性，"约束是否漂移"就再也测不准了。

## 分层

七层，依赖只能单向（见 `docs/ARCHITECTURE.md`）：

```
core → assets / audio / planner → render / container → pipeline
```

- `planner` 与 `render` **不得互相依赖**：计划是"要什么"，渲染是"怎么画"。
- `container` **不得** import `render` / `planner` / `audio`：它只接收现成的帧与 Timeline。

## 素材与容器的取舍

- **素材格式限定 PNG。** `load_png` 读 `write_png` 写出的 8 位 RGBA PNG；
  调色板 / 灰度 / 16 位 PNG 抛 `DrawvidError`。纯标准库做完整 PNG 解码不现实，
  这是明确的工程取舍，不是缺陷。
- **字幕不烧进画面。** 中文文本渲染需要字体光栅化，纯标准库做不到。
  字幕走 WebVTT 旁挂 + 播放页 `<track>`；画面内只画**一条字幕提示带**（纯色矩形），不画字。
- **容器是 APNG。** MP4 / WebM 需要真正的视频编码器；APNG 纯标准库可编可解，
  浏览器与主流播放器都能播。
- **默认 640×360 @ 30fps。** 纯 Python 渲染 1080p 全片会产生数 GB 帧与分钟级 CPU，
  那不是本项目要测的东西。分辨率与 fps 都是 `Timeline` 的字段，可配置。

## 测试

```bash
python -m pytest tests -q          # 自测套件（触达每一个公开符号）
python -m pytest tests/frozen -q   # 冻结一致性测试（只读契约）
```

`tests/frozen/` 是只读契约，不得修改、删除、重命名或跳过。

## 四条被明确否决的路线

1. **不 shell out ffmpeg（或任何外部进程）出片。** 引入外部二进制后，
   冻结一致性、覆盖率、依赖图、分层检查全部退化成"看这台机器装了什么"。
2. **不用 LLM 或任何生成式模型解析那句话。** 破坏可复现性。
3. **不把 planner 与 render 合并。** 混在一起后计划逻辑无法单独测试。
4. **不把字幕烧成像素。** 需要字体光栅化，且会与 `subs.vtt` 形成两套不一致的真源。
