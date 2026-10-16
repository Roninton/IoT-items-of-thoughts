"""规划层：一句话 + 素材路径列表 -> Timeline。

本模块只依赖 core；**不得**依赖 render —— 计划是"要什么"，渲染是"怎么画"。

解析全程由**确定性规则**完成：不调 LLM、不用随机、不读时钟、不依赖当前目录。
同一句话两次跑必须得到逐字相同的 Timeline，否则冻结测试无法客观判定。
"""

from __future__ import annotations

import os.path
import re

from .core import AudioEvent, CaptionCue, Scene, Size, Timeline

DEFAULT_DURATION_MS = 12000
DEFAULT_FPS = 30
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 360
RISER_LEAD_MS = 500
CAPTION_CHUNK_CHARS = 20
EFFECT_IN = "kenburns_in"
EFFECT_OUT = "kenburns_out"
EFFECT_STATIC = "static"

_DURATION_RE = re.compile(r"(\d+)\s*秒")
_TITLE_RE = re.compile(r"「([^」]*)」|[\"“]([^\"”]*)[\"”]")
_CAPTION_SPLIT_RE = re.compile(r"[，。！？；：、,.!?;:\n]+")


def _parse_duration_ms(sentence: str) -> int:
    """规则 1：句中 `N 秒` -> N*1000 ms；否则默认 12000 ms。"""
    matched = _DURATION_RE.search(sentence)
    if matched is None:
        return DEFAULT_DURATION_MS
    return int(matched.group(1)) * 1000


def _parse_title(sentence: str) -> str:
    """规则 7：取 `「…」` 或 `"…"` 内的内容；没有则为空字符串。"""
    matched = _TITLE_RE.search(sentence)
    if matched is None:
        return ""
    return matched.group(1) if matched.group(1) is not None else matched.group(2)


def _split_captions(sentence: str) -> list[str]:
    """规则 6 前半：按标点与换行切分 -> 去空段 -> 超 20 字硬切 -> 兜底整句。"""
    segments = [segment.strip() for segment in _CAPTION_SPLIT_RE.split(sentence)]
    pieces: list[str] = []
    for segment in segments:
        if not segment:
            continue
        for start in range(0, len(segment), CAPTION_CHUNK_CHARS):
            pieces.append(segment[start : start + CAPTION_CHUNK_CHARS])
    if not pieces:
        pieces = [sentence]
    return pieces


def _make_captions(pieces: list[str], duration_ms: int) -> list[CaptionCue]:
    """规则 6 后半：把 L 段字幕均匀铺满时间轴，第 i 段占 [i*D//L, (i+1)*D//L)。"""
    total = len(pieces)
    cues: list[CaptionCue] = []
    for index, text in enumerate(pieces):
        start_ms = index * duration_ms // total
        end_ms = (index + 1) * duration_ms // total
        cues.append(CaptionCue(start_ms=start_ms, end_ms=end_ms, text=text))
    return cues


def _scene_effect(index: int, scene_count: int) -> str:
    """规则 5：镜头数 > 1 时偶数下标 in、奇数下标 out；只有 1 个镜头时 static。"""
    if scene_count <= 1:
        return EFFECT_STATIC
    return EFFECT_IN if index % 2 == 0 else EFFECT_OUT


def _make_scenes(asset_paths: list[str], duration_ms: int) -> list[Scene]:
    """规则 2/3/4/5：按 basename 字典序排素材，均分时长，最后一个吸收余数。"""
    ordered = sorted(asset_paths, key=lambda path: (os.path.basename(path), path))
    if not ordered:
        ordered = [""]
    scene_count = len(ordered)
    scenes: list[Scene] = []
    for index, asset_path in enumerate(ordered):
        start_ms = index * duration_ms // scene_count
        end_ms = (index + 1) * duration_ms // scene_count
        scenes.append(
            Scene(
                asset_path=asset_path,
                start_ms=start_ms,
                duration_ms=end_ms - start_ms,
                effect=_scene_effect(index, scene_count),
            )
        )
    return scenes


def _make_events(scenes: list[Scene]) -> list[AudioEvent]:
    """规则 8：每个镜头起点一个 hit；下标 > 0 的镜头起点前 500ms 一个 riser。"""
    events: list[AudioEvent] = []
    for index, scene in enumerate(scenes):
        events.append(AudioEvent(t_ms=scene.start_ms, kind="hit"))
        if index > 0:
            riser_ms = scene.start_ms - RISER_LEAD_MS
            if riser_ms >= 0:
                events.append(AudioEvent(t_ms=riser_ms, kind="riser"))
    return events


def plan_from_sentence(sentence: str, asset_paths: list[str]) -> Timeline:
    """把一句话与素材列表按 P 的 9 条规则确定性地规划成 Timeline。"""
    duration_ms = _parse_duration_ms(sentence)
    scenes = _make_scenes(list(asset_paths), duration_ms)
    return Timeline(
        fps=DEFAULT_FPS,
        size=Size(DEFAULT_WIDTH, DEFAULT_HEIGHT),
        scenes=scenes,
        captions=_make_captions(_split_captions(sentence), duration_ms),
        events=_make_events(scenes),
        title=_parse_title(sentence),
    )
