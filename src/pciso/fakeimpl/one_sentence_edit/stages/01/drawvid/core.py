"""核心数据模型与异常体系。

本模块是依赖图的**叶子**：不得 import 任何其他 drawvid 子模块。
整条链路的唯一事实来源是 `Timeline`，画面、字幕、音频、清单四路输出都由它驱动。
"""

from __future__ import annotations

import json
from dataclasses import dataclass


class DrawvidError(Exception):
    """drawvid 全部异常的基类。"""


class CodecError(DrawvidError):
    """编码或解码失败。"""


@dataclass(frozen=True)
class Size:
    """画布尺寸，单位像素。"""

    width: int
    height: int


@dataclass(frozen=True)
class Scene:
    """一个镜头：某段素材在时间轴上占据的区间。

    `asset_path` 为空字符串表示纯图形镜头（不画素材）；
    `effect` 取值限于 "static" / "kenburns_in" / "kenburns_out"。
    """

    asset_path: str
    start_ms: int
    duration_ms: int
    effect: str


@dataclass(frozen=True)
class CaptionCue:
    """一条字幕，覆盖 `[start_ms, end_ms)` 区间。"""

    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class AudioEvent:
    """一个音频事件，`kind` 取值限于 "hit" / "riser"。"""

    t_ms: int
    kind: str


def _scenes_to_json(scenes: list[Scene]) -> list[dict[str, object]]:
    """把镜头列表转成稳定键序的普通结构。"""
    return [
        {
            "asset_path": scene.asset_path,
            "start_ms": scene.start_ms,
            "duration_ms": scene.duration_ms,
            "effect": scene.effect,
        }
        for scene in scenes
    ]


def _captions_to_json(captions: list[CaptionCue]) -> list[dict[str, object]]:
    """把字幕列表转成稳定键序的普通结构。"""
    return [
        {"start_ms": cue.start_ms, "end_ms": cue.end_ms, "text": cue.text}
        for cue in captions
    ]


def _events_to_json(events: list[AudioEvent]) -> list[dict[str, object]]:
    """把音频事件列表转成稳定键序的普通结构。"""
    return [{"t_ms": event.t_ms, "kind": event.kind} for event in events]


@dataclass(frozen=True)
class Timeline:
    """整条链路的唯一事实来源：帧、字幕、音频、清单全部由它推导。"""

    fps: int
    size: Size
    scenes: list[Scene]
    captions: list[CaptionCue]
    events: list[AudioEvent]
    title: str

    def duration_ms(self) -> int:
        """返回总时长：最后一个镜头的终点；没有镜头时为 0。"""
        if not self.scenes:
            return 0
        last = self.scenes[-1]
        return last.start_ms + last.duration_ms

    def frame_count(self) -> int:
        """返回帧数：`ceil(duration_ms * fps / 1000)`。"""
        return -(-self.duration_ms() * self.fps // 1000)

    def to_json(self) -> str:
        """返回稳定键序的 JSON 文本；同一 Timeline 两次调用逐字相同。"""
        payload: dict[str, object] = {
            "fps": self.fps,
            "size": {"width": self.size.width, "height": self.size.height},
            "duration_ms": self.duration_ms(),
            "frame_count": self.frame_count(),
            "title": self.title,
            "scenes": _scenes_to_json(self.scenes),
            "captions": _captions_to_json(self.captions),
            "events": _events_to_json(self.events),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
