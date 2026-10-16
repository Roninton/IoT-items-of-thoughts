# 架构说明

## 分层与依赖方向

```
core        叶子层：Timeline / Scene / CaptionCue / AudioEvent / Size / 异常。
            不得 import 任何其他 drawvid 子模块。
assets      素材层：Image、PNG 编解码、最近邻缩放。只可依赖 core。
audio       音频层：按时间轴合成 BGM（WAV）。只可依赖 core。
planner     规划层：一句话 -> Timeline（确定性规则）。只可依赖 core。
render      渲染层：render_frame / render_frames。可依赖 core、assets。
container   容器层：APNG、WebVTT、播放页。可依赖 core、assets。
pipeline    编排层：串联以上各层，含 CLI 与 demo。可依赖全部。
```

依赖只能单向，下层不得 import 上层：

| 模块 | 可以依赖 |
|---|---|
| `drawvid.core` | — |
| `drawvid.assets` | `core` |
| `drawvid.audio` | `core` |
| `drawvid.planner` | `core` |
| `drawvid.render` | `core`、`assets` |
| `drawvid.container` | `core`、`assets` |
| `drawvid.pipeline` | `core`、`assets`、`audio`、`planner`、`render`、`container` |

两条关键的反向禁止：

- **`planner` 与 `render` 互不依赖。** 计划是"要什么"，渲染是"怎么画"。
  混在一起后，计划逻辑无法单独测试，渲染也无法脱离文本输入复用。
- **`container` 不得 import `render` / `planner` / `audio`。** 它只接收现成的帧与
  Timeline，不关心画面怎么算出来、声音怎么合成。

全部 import 都是相对 import（`from .core import Timeline`）。

## 各层职责

### core —— 时间轴数据模型

`Size` / `Scene` / `CaptionCue` / `AudioEvent` 都是 frozen dataclass；
`Timeline` 是整条链路的唯一事实来源：

- `duration_ms()` = 最后一个 scene 的 `start_ms + duration_ms`；无 scene 时为 0
- `frame_count()` = `ceil(duration_ms * fps / 1000)`
- `to_json()` 输出稳定键序的 JSON，同一 Timeline 两次调用逐字相同

素材只以**路径字符串**出现在 `Scene.asset_path` 上；图像本身由 `assets` 层按需加载。

### assets —— 纯标准库 PNG

- `Image` 内部是一块紧凑的 8 位 RGBA `bytearray`，`get_pixel` / `put_pixel` 越界钳到边缘。
- `load_png` 手写解析：签名 → IHDR → IDAT → IEND，逐块校验 CRC，
  解压后还原 filter 0–4（None / Sub / Up / Average / Paeth）。
  只支持 8 位真彩 RGBA；调色板 / 灰度 / 16 位 / Adam7 一律抛 `DrawvidError`。
- `write_png` 写 8 位 RGBA，逐行 filter 固定用 0（None）：慢一点点，但确定性、
  可读、且与 `load_png` 逐像素往返一致。每块 CRC 由 `zlib.crc32` 算出。
- `resize` 是最近邻缩放，结果只由入参决定。

### planner —— 一句话变时间轴

解析全程由确定性规则完成（不调 LLM、不用随机、不读时钟）。九条规则：

1. **时长**：句中 `N 秒` → `N * 1000` ms；否则默认 `12000` ms。
2. **素材顺序**：按 `os.path.basename` 字典序排序。
3. **镜头数**：等于素材数；素材为空则 1 个纯图形镜头（`asset_path=""`）。
4. **镜头时长**：总时长均分，最后一个镜头吸收余数，保证首尾相接。
5. **效果**：镜头数 > 1 时偶数下标 `kenburns_in`、奇数下标 `kenburns_out`；单个镜头 `static`。
6. **字幕**：按中英标点与换行切分 → 去空段 → 每段超 20 字硬切 → 空则整句。
   然后均匀铺满时间轴：第 i 段占 `[i*D//L, (i+1)*D//L)`。
7. **标题**：取 `「…」` 或 `"…"` 内的内容；没有则为空字符串。
8. **音频事件**：每个 scene 起点一个 `hit`；下标 > 0 的 scene 起点前 500 ms 一个 `riser`。
9. `fps = 30`，`size = Size(640, 360)`。

### render —— 时间即画面

`render_frame(timeline, t_ms, images)` 是纯函数：

1. `t_ms` 钳到 `[0, duration_ms - 1]`，找到覆盖它的 scene，`p = (t - start) / duration`。
2. **背景**：以 scene 下标为唯一变量的确定性渐变，三通道都落在 `[16, 200]`
   （不得出现纯白 —— 字幕带断言依赖这一点）。
3. **素材**：若 `images` 里有该 `asset_path`，按 `effect` 做铺满画面的中心锚点位移缩放
   （`kenburns_in` 1.0 → 1.06，`kenburns_out` 1.06 → 1.0，`static` 不变）。
4. **进度条**：底部 `[H-4, H)` 全宽，已完成部分填白。完成比例用纯整数运算的
   加速缓动曲线 `x + 6.25 * x^3`（`x = t / 时长`）：起点仍有非零斜率、整体单调递增，
   只在收尾处比线性更快铺满。
5. **字幕带**：有 cue 覆盖时，把 `x ∈ [W//10, W*9//10]`、
   `y ∈ [int(H*0.82), int(H*0.90))` 的矩形填白；没有 cue 覆盖时不画。

绘制顺序是**字幕带先、进度条后**：小画布上两者会重叠，进度条压在上层，
这样"进度条只由时间决定"这条性质在任何尺寸下都成立。

`render_frames` 返回 `frame_count()` 张帧，第 i 张取 `t = i * 1000 // fps`。

### container —— 多路输出里的"壳"

- `encode_apng` 写**真 APNG**：`IHDR` → `acTL`（帧数 + 循环次数，0 = 无限）→
  每帧一个 `fcTL`，第 1 帧数据在 `IDAT`、第 2 帧起在 `fdAT` → `IEND`。
  每块 CRC 由 `zlib.crc32` 算出。空帧列表抛 `DrawvidError`。
- `write_vtt` 写 WebVTT：首行 `WEBVTT`，每条 caption 一个 cue，
  时间戳格式 `hh:mm:ss.mmm --> hh:mm:ss.mmm`。
- `write_player` 生成播放页：`<img>` 按 `fps` 步进 `frames/frame_%04d.png`，
  `<track>` 挂 `subs.vtt`，`<audio>` 挂 `bgm.wav`，三者共用同一条时间轴。

### audio —— 代码合成器乐

`wave` + `math`，单声道 / 16 位 / 22050Hz，采样数 = `duration_ms * 22050 // 1000`。
内容由时间轴确定：和弦垫音按 scene 下标在和弦间循环，`hit` 是起点的衰减脉冲，
`riser` 是切换前 500 ms 的上行扫频。同一 Timeline 两次调用逐字节相同。

### pipeline —— 编排

`make_video(sentence, assets_dir, out_dir)` 按后缀过滤素材、调 `planner` 规划、
按需 `load_png`、`render_frames` 渲染、逐帧 `write_png`、`encode_apng` 封装，
再写 `plan.json` / `subs.vtt` / `bgm.wav` / `index.html`，返回 `movie.apng` 的路径。

## 确定性清单

`render_frame` 与 `synthesize_bgm` 都不：

- 读时钟（无 `time`）
- 用随机（无 `random`）
- 依赖当前工作目录或环境变量
- 保留跨调用状态
- 在渲染里做 I/O

因此同一输入跨进程、跨次运行产出逐字节相同的产物。`tests/test_drawvid.py` 里有
复现性回归（两次 `make_video` 的 `movie.apng` 与 `bgm.wav` 逐字节相同）。
