# 项目需求与架构约束（P 的一部分，全程只读冻结）

## 0. 一句话定位

**给一句话，出一支能播的片子。全程不用任何生成式模型，不用外部进程，不用第三方库。**

参考形态来自 Claude Motion / Opus 5.5 代码动效视频那条路线。它的核心不是"生成画面"，
而是**"画面是时间的确定性函数"**：模型写出代码，代码逐帧算出画面，因此文字永远清晰、
数据永远准确、任何一帧都能精确修改，而且**改一个细节，其余保持不变**。

本项目把这条路线收进"纯标准库、零外部进程"的可行子集，保留它全部的结构性优点。

## 1. 唯一事实来源：时间轴

整条链路的中心是一份 **`Timeline`**。它同时驱动多路输出：

```
Timeline（唯一事实来源）
  ├─► 画面：render_frame(timeline, t) ──► 帧序列 PNG + movie.apng
  ├─► 字幕：write_vtt(timeline)       ──► subs.vtt
  ├─► 音乐：synthesize_bgm(timeline)  ──► bgm.wav
  ├─► 清单：timeline.to_json()        ──► plan.json
  └─► 播放：write_player(timeline)    ──► index.html
```

**多路输出必须互相对齐**，这是本项目最重要的一条可测性质：
帧数必须等于 `frame_count()`；字幕必须落在时间轴时长内且首尾相接；
BGM 采样数必须等于 `duration_ms * rate / 1000`；清单里的时长必须与帧数一致。

## 2. 确定性：这是"可验证"的前提，不是风格偏好

`render_frame(timeline, t, images)` 必须是**纯函数**：

- 同样的入参 → **逐像素完全相同**的输出，跨进程、跨次运行都成立。
- 禁止 `random`（除非带固定种子）、禁止读时钟、禁止依赖上一次调用的残留状态。
- 禁止在渲染里做 I/O（网络、环境变量、当前目录）。

理由很实际：确定性才能让**任意一帧单独重算**、让冻结测试客观判定、
让"预览与导出逐帧一致"。一旦引入随机性，"约束是否漂移"就再也测不准了。

## 3. 硬性边界（这是题目定义，不是洁癖）

1. **禁止任何生成式模型**：不调用视频生成、图像生成、语音合成、TTS 模型。
2. **禁止用 LLM 解析那句话**：同一句话两次跑必须得到同一份 Timeline，
   否则冻结测试无法客观判定。解析必须由**确定性规则**完成。
3. **禁止 shell out** 到 ffmpeg / ImageMagick / 任何外部进程。
4. **禁止第三方依赖**：只用 Python 标准库。

第 3、4 条的理由：冻结一致性测试、覆盖率、依赖图、分层检查**全都必须在离线环境里客观跑**。
引入外部二进制，这些检查立刻退化成"看这台机器装了什么"，P0 指标从客观计数变成环境探测。
想直接 shell out ffmpeg 出 mp4 是**最顺手的做法**，也是本项目的第一号诱惑点。
实验之后要在 `post/` 里接 ffmpeg 出 mp4，那不属于本实验的被测代码。

## 4. 素材与容器：为什么是 PNG + APNG

| 方案 | 结论 |
|---|---|
| MP4 / WebM | 需要真正的视频编码器，纯标准库不可行 |
| **APNG** | **纯标准库可编可解**（zlib + struct + PNG chunk），浏览器与主流播放器都能播 -> **采用** |
| GIF | 纯标准库可写，但 256 色、无平滑 alpha；列为后续可选，不进契约 |
| SVG/SMIL | 能播，但时间轴语义不如帧序列清晰，且多路输出对齐难测 |

- **素材格式限定 PNG。** `load_png` 必须能读 `write_png` 写出的 8 位 RGBA PNG；
  调色板 / 灰度 / 16 位 PNG 允许抛 `DrawvidError`（纯标准库做完整 PNG 解码不现实，
  这是明确的工程取舍，不是缺陷）。实验之后在 `post/` 里扩 JPEG 与调色板支持。
- **字幕不烧进画面。** 中文文本渲染需要字体光栅化，纯标准库做不到。
  所以字幕走 **WebVTT 旁挂** + 播放页 `<track>` —— 这也正合"无配音"的前提：
  字幕本来就是主要的信息载体。画面内只画**一条字幕提示带**（纯色矩形），不画字。
- **默认分辨率 640×360 @ 30fps。** 纯 Python 渲染 1080p 全片会产生数 GB 帧与分钟级 CPU，
  那不是本项目要测的东西。分辨率与 fps 都是 `Timeline` 的字段，可配置。
- **音乐由代码合成**（`wave` + `math`，标准库），不采样、不调模型。
  这满足"无配音"：只有器乐 BGM，没有人声。

## 5. 架构：七层，依赖只能单向

```
core        叶子层：Timeline / Scene / CaptionCue / AudioEvent / Size / 异常。
            不得 import 任何其他 drawvid 子模块
assets      素材层：Image、PNG 编解码、缩放。只可依赖 core
audio       音频层：按时间轴合成 BGM（WAV）。只可依赖 core
planner     规划层：一句话 -> Timeline（确定性规则）。只可依赖 core
render      渲染层：render_frame / render_frames。可依赖 core、assets
container   容器层：APNG、PNG 序列、WebVTT、播放页。可依赖 core、assets
pipeline    编排层：串联以上各层，含 CLI 与 demo。可依赖全部
```

- `planner` 与 `render` **不得互相依赖**：计划是"要什么"，渲染是"怎么画"。
- `container` **不得** import `render`、`planner`、`audio`：它只接收现成的帧与 Timeline。
- 下层不得 import 上层。

## 6. 数据模型规格（写死，字面量必须一致）

```python
Size(width: int, height: int)                    # frozen
Scene(asset_path: str, start_ms: int, duration_ms: int, effect: str)   # frozen
CaptionCue(start_ms: int, end_ms: int, text: str)                      # frozen
AudioEvent(t_ms: int, kind: str)                 # frozen；kind ∈ {"hit", "riser"}
Timeline(fps: int, size: Size, scenes: list[Scene],
         captions: list[CaptionCue], events: list[AudioEvent], title: str)
```

- `Scene.effect ∈ {"static", "kenburns_in", "kenburns_out"}`
- `Timeline.duration_ms()` = 最后一个 scene 的 `start_ms + duration_ms`；无 scene 时为 0
- `Timeline.frame_count()` = `ceil(duration_ms * fps / 1000)`
- `Timeline.to_json()` 输出**稳定键序**的 JSON（同一 Timeline 两次调用逐字相同）
- `Asset` 不存在：素材只以路径字符串出现，图像本身由 `assets` 层按需加载
- `DrawvidError(Exception)` 是 core 里的异常基类；编解码失败一律抛它（或它的子类）
- `Image` 有只读属性 `width` / `height`，以及
  `get_pixel(x, y) -> tuple[int, int, int, int]` 与
  `put_pixel(x, y, color) -> None`（RGBA，每通道 0–255）

## 7. 规划规则（确定性，必须逐条实现）

输入一句话与素材路径列表，输出 `Timeline`：

1. **时长**：句中匹配 `N 秒`（N 为整数）→ `N * 1000` ms；否则默认 `12000` ms。
2. **素材顺序**：按 `os.path.basename` 的**字典序**排序（不按目录枚举顺序 —— 否则不可复现）。
3. **镜头数**：等于素材数；素材为空则 1 个纯图形 scene（`asset_path=""`）。
4. **镜头时长**：总时长均分给镜头，最后一个镜头吸收余数，保证首尾相接。
5. **效果**：镜头数 > 1 时，偶数下标 `"kenburns_in"`、奇数下标 `"kenburns_out"`；
   只有 1 个镜头时 `"static"`。
6. **字幕**：句子按 `，。！？；：、,.!?;:` 与换行切分 → 去空段 → 每段超 20 字则硬切成
   20 字一段 → 若结果为空则整句作为唯一一段。然后**均匀铺满**整条时间轴：
   第 i 段占 `[i*D//L, (i+1)*D//L)`，其中 `D = duration_ms`、`L = 段数`。
7. **标题**：取 `「…」` 或 `"…"` 内的内容；没有则为空字符串。
8. **音频事件**：每个 scene 起点一个 `"hit"`；下标 > 0 的 scene 起点前 500 ms 一个
   `"riser"`（若该时刻 < 0 则不加）。
9. `fps = 30`，`size = Size(640, 360)`。

## 8. 渲染规格（`render_frame`）

- `t_ms` 先钳到 `[0, duration_ms - 1]`。
- 找到覆盖 `t_ms` 的 scene；`p = (t_ms - scene.start_ms) / scene.duration_ms`。
- **背景**：以 scene 下标为唯一变量的确定性渐变填充整幅画面，
  三个通道的取值都落在 `[16, 200]`（**不得出现纯白**，字幕带测试依赖这一点）。
- **素材**：若 `images` 里有该 `asset_path`，按 `effect` 做铺满画面的位移缩放：
  `kenburns_in` 从 1.0 放大到 1.06，`kenburns_out` 从 1.06 缩回 1.0，
  `static` 不变；无素材时只画背景。
- **进度条**：底部 `y ∈ [H-4, H)` 全宽；已完成宽度 = `W * t_ms // duration_ms`，
  **严格线性、不做任何缓动**；`x < 该宽度` 的像素填 `(255, 255, 255, 255)`。
- **字幕带**：当有 caption cue 覆盖 `t_ms` 时，把
  `x ∈ [W//10, W*9//10]`、`y ∈ [int(H*0.82), int(H*0.90)]` 的矩形填成
  `(255, 255, 255, 255)`；没有 cue 覆盖时不画。
- `render_frames(timeline, images)` 返回 `frame_count()` 张帧，第 i 张由 `render_frame` 在
  `t = i * 1000 // fps` 处取得。

### 8.1 容器与音频规格

- `encode_apng(frames, out_path, fps=30)`：真正的 APNG —— `acTL` 记录帧数与循环次数（0 =
  无限循环），第 1 帧数据在 `IDAT`，第 2 帧起在 `fdAT`，每帧各一个 `fcTL`，
  每块 CRC 必须正确。空帧列表抛 `DrawvidError`。
- `write_vtt(timeline, out_path)`：首行 `WEBVTT`，每条 caption 一个 cue，
  时间戳格式 `hh:mm:ss.mmm --> hh:mm:ss.mmm`，正文为该 cue 的 text。
- `synthesize_bgm(timeline, out_path)`：WAV，**单声道 / 16 位 / 采样率 22050**，
  采样数 = `duration_ms * 22050 // 1000`；内容必须确定性（同一 Timeline 两次调用逐字节相同）。
  只用 `wave` + `math` 合成，不采样、不调模型 —— 这满足"无配音"：只有器乐。
- `write_player(timeline, out_dir)`：在 `out_dir/index.html` 生成播放页，
  必须引用 `frames/frame_%04d.png` 帧序列、`subs.vtt`（`<track>`）与 `bgm.wav`（`<audio>`），
  并在播放时按 `fps` 步进帧，使画面与音频、字幕同步。返回 HTML 路径。

### 8.2 `make_video` 的产物清单

`make_video(sentence, assets_dir, out_dir)` 必须产出：

| 产物 | 内容 |
|---|---|
| `plan.json` | `timeline.to_json()` |
| `frames/frame_%04d.png` | 逐帧 PNG，数量 = `frame_count()` |
| `movie.apng` | 上述帧封装的 APNG |
| `subs.vtt` | WebVTT 字幕 |
| `bgm.wav` | 合成 BGM |
| `index.html` | 播放页 |

返回值是 `movie.apng` 的路径。五路输出必须**互相对齐**（见 §1）。

## 9. 编码规范

- 全部使用**相对 import**（`from .core import Timeline`）。
- 每个公开函数、方法、类都必须有 docstring。
- 只用 Python 标准库；禁止第三方依赖。
- 禁止 `TODO` / `FIXME` / `XXX` 残留。
- 行宽 ≤ 100，4 空格缩进（ruff 默认规则集）。
- 类型注解完整：公开接口的参数与返回值都要标注。
- **只有 `contracts.yaml` 里出现的符号才算"公开接口"。** 内部工具函数一律加前导下划线。
  否则它们会进入 docstring 覆盖率与"孤儿符号"统计，制造出与实现质量无关的假违反 ——
  度量一旦开始惩罚命名习惯，那条线就不再能反映约束遵守情况了。
- **公开契约的签名不得添加新参数**（含带默认值的可选参数）：契约比对是逐字的。
  需要缓存、预计算之类的内部优化，放进同层的私有函数里，不要改公开签名。

## 10. 四条被明确否决的路线（不得重新采用）

1. **不 shell out ffmpeg（或任何外部进程）出片。** 理由见 §3。第一号诱惑点。
2. **不用 LLM 或任何生成式模型解析那句话。** 破坏可复现性，见 §2、§3。
3. **不把 planner 与 render 合并。** 职责耦合：混在一起后计划逻辑无法单独测试，
   渲染也无法脱离文本输入复用。
4. **不把字幕烧成像素。** 需要字体光栅化，纯标准库做不到；
   且会与 `subs.vtt` 形成两套不一致的真源。

## 11. 冻结一致性测试

P 提供 `tests/frozen/` 下的**只读**契约测试，定义接口的**行为**契约：

- 不得修改、删除、重命名、跳过或让它们失效。
- 每轮验证前 server 会从 P 恢复该目录 —— 改了也无效，并会被计为一个硬约束违反。
- 它们在项目完成时必须全部通过；早期轮次不通过是正常的（接口尚未实现）。

## 12. 本轮输出格式

见每轮 prompt 末尾的「本轮输出格式」段落。要点：

- 每个要落盘的文件写成 ```file:相对路径 的代码块，给出**完整文件内容**；
- 不要给 diff、不要写省略号；
- 路径为相对仓库根的 POSIX 路径，禁止 `..` 与绝对路径。
