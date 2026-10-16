"""视频编解码与时间轴：纯 Python 容器编码 + 多片段串接。

本模块只依赖依赖图的叶子 :mod:`drawvid.core`。
"""

from __future__ import annotations

import abc
import io
import zipfile
from dataclasses import dataclass, field

from .core import CodecError


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


class ZipCodec(Codec):
    """把帧序列编码为 zip 容器的编解码器。

    纯 Python 容器编码，刻意不使用 ffmpeg 子进程：子进程会引入外部依赖并让编码器不可测。
    """

    name = "zip"

    def encode(self, sequence: FrameSequence) -> bytes:
        """把每帧写成 frame_0000.svg 这类四位序号成员，并附带记录 fps 与帧数的 manifest.txt。"""
        if len(sequence) == 0:
            raise CodecError("空帧序列无法编码")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for position in range(len(sequence)):
                frame = sequence.frame_at(position)
                name = f"frame_{position:04d}.svg"
                archive.writestr(name, frame.payload.encode("utf-8"))
            manifest = f"fps={sequence.fps}\nframes={len(sequence)}\n"
            archive.writestr("manifest.txt", manifest.encode("utf-8"))
        return buffer.getvalue()

    def decode(self, blob: bytes) -> FrameSequence:
        """从 zip 容器恢复帧序列，并按 manifest.txt 还原帧率。"""
        try:
            with zipfile.ZipFile(io.BytesIO(blob), "r") as archive:
                names = sorted(
                    name
                    for name in archive.namelist()
                    if name.lower().endswith(".svg")
                )
                frames: list[Frame] = []
                for position, name in enumerate(names):
                    payload = archive.read(name).decode("utf-8")
                    frames.append(Frame(index=position, payload=payload))
                fps = 12
                if "manifest.txt" in archive.namelist():
                    text = archive.read("manifest.txt").decode("utf-8")
                    for raw in text.splitlines():
                        key, _, value = raw.partition("=")
                        if key.strip() == "fps" and value.strip():
                            fps = int(value.strip())
        except zipfile.BadZipFile as exc:
            raise CodecError(f"不是合法的 zip 容器：{exc}") from exc
        sequence = FrameSequence(fps=fps)
        for frame in frames:
            sequence.append(frame)
        return sequence


@dataclass
class Clip:
    """时间轴上的一个片段：帧序列 + 起始毫秒。"""

    sequence: FrameSequence
    start_ms: int

    def end_ms(self) -> int:
        """返回片段结束时刻（起始毫秒 + 序列时长）。"""
        return self.start_ms + self.sequence.duration_ms()


class Timeline:
    """按时间摆放多个片段，并渲染成一条连续帧序列。"""

    def __init__(self) -> None:
        """创建一个空时间轴。"""
        self._clips: list[Clip] = []

    def add_clip(self, sequence: FrameSequence, start_ms: int = 0) -> int:
        """加入一个片段，返回其序号。"""
        self._clips.append(Clip(sequence=sequence, start_ms=start_ms))
        return len(self._clips) - 1

    def duration_ms(self) -> int:
        """返回全部片段结束时刻的最大值，空时间轴返回 0。"""
        if not self._clips:
            return 0
        return max(clip.end_ms() for clip in self._clips)

    def render(self) -> FrameSequence:
        """按起始时刻排序，把各片段的帧依次串接成一条序列。"""
        ordered = sorted(self._clips, key=lambda clip: clip.start_ms)
        fps = ordered[0].sequence.fps if ordered else 12
        merged = FrameSequence(fps=fps)
        position = 0
        for clip in ordered:
            for offset in range(len(clip.sequence)):
                frame = clip.sequence.frame_at(offset)
                merged.append(Frame(index=position, payload=frame.payload))
                position += 1
        return merged
