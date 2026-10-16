"""视频抽象接口：帧、帧序列与编解码器协议。

本模块只依赖依赖图的叶子 :mod:`drawvid.core`。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Frame:
    """一帧视频：帧号与已渲染好的画面负载。"""

    index: int
    payload: str


@dataclass
class FrameSequence:
    """按顺序保存的一组帧，附带帧率。"""

    fps: int = 12
    _frames: list[Frame] = field(default_factory=list, repr=False)

    def append(self, frame: Frame) -> int:
        """追加一帧，返回它所在的位置索引。"""
        self._frames.append(frame)
        return len(self._frames) - 1

    def __len__(self) -> int:
        """返回序列中的帧数。"""
        return len(self._frames)

    def frame_at(self, index: int) -> Frame:
        """按位置索引取帧，索引越界时由列表本身报错。"""
        return self._frames[index]

    def duration_ms(self) -> int:
        """按帧率换算总时长（毫秒，向上取整）。"""
        return int(len(self._frames) * 1000 / self.fps + 0.999)


class Codec(abc.ABC):
    """编解码器抽象基类：定义帧序列与字节流之间的双向转换。"""

    name = "abstract"

    @abc.abstractmethod
    def encode(self, sequence: FrameSequence) -> bytes:
        """把帧序列编码成字节流。"""

    @abc.abstractmethod
    def decode(self, blob: bytes) -> FrameSequence:
        """把字节流解码回帧序列。"""
