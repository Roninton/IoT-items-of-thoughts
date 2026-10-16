# drawvid

`drawvid` 是一个纯 Python 的**绘图工具箱 + 视频生成工具链**：先把点、线段、矩形轮廓光栅化成整数像素路径，
把路径累积到内存画布上导出 SVG，再把多帧 SVG 打包成 zip 容器，并支持用时间轴把多个片段串接成一条视频。

整个项目**只使用 Python 标准库**，不调用 ffmpeg 或任何外部进程，因此可以离线、可重复地端到端跑通与单测。

## 安装

项目无第三方依赖，只需 Python 3.11 及以上版本：

```
python --version
```

把它放到 `PYTHONPATH` 上即可使用（仓库内的最终目录结构见下表）：

```
set PYTHONPATH=<仓库根>
python -c "import drawvid; print(drawvid.__version__)"
```

## 运行自测

自测套件只依赖 `pytest` 本身，测试文件位于 `tests/test_drawvid.py`：

```
set PYTHONPATH=<仓库根>
python -m pytest tests -q
```

## 模块结构

| 模块 | 职责 | 允许依赖 |
| --- | --- | --- |
| `drawvid.core` | 核心类型与错误：`Point`、`Rect`、`DrawvidError`、`CodecError`；依赖图的叶子 | 无 |
| `drawvid.drawing` | 绘图基元（`line`、`rect`、`ellipse`）与内存画布 `Canvas`（导出 SVG） | `core` |
| `drawvid.video` | 视频抽象接口（`Frame`、`FrameSequence`、`Codec`）、zip 容器编码器 `ZipCodec`、时间轴 `Timeline` | `core` |
| `drawvid.pipeline` | 集成层：画布 → 帧序列 → 视频文件 | `core`、`drawing`、`video` |

依赖方向是单向的，`core` 是叶子，不允许出现反向依赖或循环 import。分层理由与被否决的替代方案见
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

## `demo.py` 用法

`demo.py` 是一条最小端到端链路：建一个 64x64 画布，加一条矩形轮廓路径，渲染 4 帧，写入临时目录里的 zip，
再用 `ZipCodec` 解码回来校验，并打印统计信息。

```
set PYTHONPATH=<仓库根>
python demo.py
```

预期输出形如：

```
frames=4 duration_ms=334 decoded=4 bytes=765
```

其中 `frames` 是编码前的帧数，`decoded` 是解码回来的帧数（两者相等即表示往返无损），`bytes` 是 zip 容器的字节数。
