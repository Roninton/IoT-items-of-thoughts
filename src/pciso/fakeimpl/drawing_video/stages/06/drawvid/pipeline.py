"""集成层：把画布渲染成帧序列，并把帧序列写成视频文件。

本模块可以依赖 :mod:`drawvid.core`、:mod:`drawvid.drawing` 与 :mod:`drawvid.video`。
"""

from __future__ import annotations

from .core import DrawvidError
from .drawing import Canvas
from .video import Frame, FrameSequence, ZipCodec


def render_canvas_to_frames(canvas: Canvas, frames: int, fps: int = 12) -> FrameSequence:
    """把画布重复渲染成 frames 帧，并在每帧的 svg 根标签上打上帧号。"""
    if frames <= 0:
        raise DrawvidError("帧数必须为正")
    sequence = FrameSequence(fps=fps)
    for i in range(frames):
        svg = canvas.to_svg()
        tagged = svg.replace("<svg ", f'<svg data-frame="{i}" ', 1)
        sequence.append(Frame(index=i, payload=tagged))
    return sequence


def build_video(seq: FrameSequence, out_path: str) -> str:
    """把帧序列编码为 zip 容器并写入 out_path，返回该路径。"""
    if len(seq) == 0:
        raise DrawvidError("空帧序列无法生成视频")
    blob = ZipCodec().encode(seq)
    with open(out_path, "wb") as handle:
        handle.write(blob)
    return out_path
