# 笔刷画画工具（brushkit）—— 需求原文（冻结，只读）

本文件就是**需求原文**。它一个字都不许改；所有实现与判断都以这里的字面为准。

## 0. 全部要求（原文）

1. 写一个**笔刷画画**的工具：在画布上用笔刷画画，并**自动输出画图结果的帧地址**。2. 每次画画的输出**固定写同一个文件**：`out/current-view.jpg` —— 这个路径就是"帧地址"。
   - 不得出现按时间戳 / 随机数 / 序号命名的图片，不得每次另存新文件；
   - 画一次覆盖一次，输出目录里的图片文件个数**恒为 1**。
3. 画笔内容**可以撤回**，并且要支持**连续撤回**（一次撤一笔，可以连着撤多笔）。
   - 撤到空画布之后继续撤：**no-op**——不报错、不写坏文件、退出码仍然是 0。
4. **不许拿随机数兜底**：同一条指令脚本跑两次，`current-view.jpg` 必须**逐字节相同**。
   即：不得读时钟、不得用随机、不得依赖 `set`/`dict` 迭代顺序等不确定来源。

## 1. 输入格式与笔刷语义（冻结）

- 画布：`width × height` 像素、RGB，初始全白 `(255,255,255)`。
- 笔刷：一次 `stroke` = 一个**圆形笔头**（半径 `radius`）从 `(x0,y0)` **直线扫到** `(x1,y1)`，
  笔头颜色 `color: [r,g,b]`，不透明度 `alpha ∈ (0,1]`，按 source-over 与画布混合。
  落在画布外的像素忽略；`radius <= 0` 视为 1。
- 指令脚本（JSON，UTF-8）：
  ```json
  {
    "canvas": {"width": 320, "height": 240},
    "ops": [
      {"kind": "stroke", "x0": 20, "y0": 20, "x1": 80, "y1": 60,
       "radius": 8, "color": [255, 0, 0], "alpha": 1.0},
      {"kind": "undo"}
    ]
  }
  ```
  `ops` 按数组顺序逐条执行；`undo` 撤销**最近一次尚未被撤销的 stroke**。
- CLI（冻结，必须能这样跑）：
  ```
  python -m brushkit --script <ops.json> [--out <dir>]
  ```
  `--out` 缺省为 `out`（相对当前工作目录）。成功退出码 0，并生成 `<out>/current-view.jpg`。
  stdout / stderr 内容不作要求；非法脚本（JSON 坏了、缺 `canvas`）必须**非 0 退出**且不写坏文件。

## 2. 分层与文件名（P 的机器可读部分在 layers.yaml / contracts.yaml，这里说清意图）

`brushkit` 是根包，五层单向；**文件名与层同名**，冻结验收测试会顶层 import 这些模块：

```
core    brushkit/core.py    Canvas / Stroke / BrushError / blank_canvas：纯像素缓冲，不认识文件与命令行
raster  brushkit/raster.py  stroke_pixels：把一条 stroke 光栅化进像素缓冲（只依赖 core）
export  brushkit/export.py  write_current_view：像素缓冲 -> <out>/current-view.jpg（只依赖 core）
undo    brushkit/undo.py    UndoStack.push / UndoStack.undo：连续撤回（只依赖 core）
cli     brushkit/cli.py     run：解析 ops.json、串起前面几层、写盘
main    brushkit/__main__.py  入口转发：from brushkit.cli import run; raise SystemExit(run())
```

`__main__` 是独立一层，**只许** import `cli`（多 import 一层就算越层，硬约束 C12）。

接口签名以 `contracts.yaml` 为准（它随 P 一起进 prompt，逐字核对）：已实现的签名不得漂移（硬约束 C2）。
`Canvas` 要有 `to_bytes() -> bytes` 与 `from_bytes(data: bytes) -> None`（撤回靠它整块还原像素缓冲）。

## 3. 编码规范

- Python 3.12+；除 **Pillow**（本机 12.3.0 可用）外不引入第三方依赖，不 shell out 外部程序。
- 每个公开函数 / 类必须有 docstring；不留 `TODO` / `FIXME`。
- 线上代码里**不得**出现 `random`、`time.time()`、`datetime.now()`、`uuid` 这类不确定性来源。
- 自测放在 `tests/`；`tests/frozen/` 是 P 提供的只读验收测试，不许改、不许删、不许跳过。

## 4. 明确否决的路线（不要走）

- 每次输出新文件名（时间戳 / 序号）来"保留帧序列" —— 直接违反要求 2，会让存储爆炸。
- 撤回 = 清空画布，或撤回 = 重画整幅 —— 前者语义错，后者把"连撤"变成"全清"。
- 用随机抖动笔头做"墨感" —— 直接违反要求 4。
- 把 `alpha` 忽略掉当不透明笔刷 —— 混合语义错，属于弱化需求。
- 为了让字节相同而换格式（例如偷偷写 PNG 叫 `.jpg`）—— 输出必须是真 JPEG。
