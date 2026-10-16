"""drawvid 自测套件：覆盖 core / drawing / video / pipeline 的契约行为。"""

from __future__ import annotations

import dataclasses
import io
import itertools
import zipfile

import pytest

from drawvid.core import CodecError, DrawvidError, Point, Rect
from drawvid.drawing import Canvas, line, rect
from drawvid.pipeline import build_video, render_canvas_to_frames
from drawvid.video import Codec, Frame, FrameSequence, Timeline, ZipCodec


def _sequence(count: int, fps: int = 12) -> FrameSequence:
    """构造 count 帧的测试序列，每帧负载标记自己的帧号。"""
    seq = FrameSequence(fps=fps)
    for i in range(count):
        seq.append(Frame(index=i, payload=f"<svg data-frame=\"{i}\"></svg>"))
    return seq


def test_point_is_frozen_value_object() -> None:
    """Point 是值对象：等值比较成立，且不可修改。"""
    point = Point(3, 4)
    assert point.as_tuple() == (3, 4)
    assert point == Point(3, 4)
    with pytest.raises(dataclasses.FrozenInstanceError):
        point.x = 9  # type: ignore[misc]


def test_rect_corners_order() -> None:
    """Rect.corners 按左上、右上、右下、左下返回四个角点。"""
    corners = Rect(1, 2, 3, 4).corners()
    assert [c.as_tuple() for c in corners] == [(1, 2), (4, 2), (4, 6), (1, 6)]


def test_line_endpoints_and_continuity() -> None:
    """line 返回含首尾端点的连续点列。"""
    points = line(0, 0, 3, 3)
    assert points[0] == Point(0, 0)
    assert points[-1] == Point(3, 3)
    assert len(points) == 4
    for a, b in itertools.pairwise(points):
        assert abs(a.x - b.x) <= 1 and abs(a.y - b.y) <= 1


def test_rect_closes_without_duplicate_corner() -> None:
    """rect 生成闭合轮廓，且角点不重复。"""
    points = rect(Rect(0, 0, 2, 2))
    assert len(points) == 8
    assert len(set(points)) == 8


def test_canvas_to_svg() -> None:
    """Canvas 累积路径并导出 SVG；路径为空或尺寸非法时报错。"""
    canvas = Canvas(8, 6)
    assert canvas.add(line(0, 0, 7, 0)) == 0
    assert canvas.path_count() == 1
    svg = canvas.to_svg()
    assert svg.startswith("<svg ")
    assert 'width="8"' in svg
    assert "<path" in svg
    assert svg.endswith("</svg>")
    with pytest.raises(DrawvidError):
        canvas.add([])
    with pytest.raises(DrawvidError):
        Canvas(0, 4)


def test_frame_sequence_append_and_index() -> None:
    """FrameSequence 追加返回位置索引，可按下标取帧，时长按帧率换算。"""
    seq = FrameSequence(fps=10)
    assert len(seq) == 0
    assert seq.duration_ms() == 0
    assert seq.append(Frame(index=0, payload="a")) == 0
    assert seq.append(Frame(index=1, payload="b")) == 1
    assert len(seq) == 2
    assert seq.frame_at(1).payload == "b"
    assert seq.duration_ms() == 200


def test_codec_is_abstract_contract() -> None:
    """Codec 是不可直接实例化的抽象编解码协议。"""
    assert isinstance(ZipCodec(), Codec)
    with pytest.raises(TypeError):
        Codec()  # type: ignore[abstract]


def test_zip_codec_roundtrip() -> None:
    """ZipCodec 往返无损，并保留帧率。"""
    seq = _sequence(3, fps=15)
    blob = ZipCodec().encode(seq)
    assert isinstance(blob, bytes)
    assert blob[:2] == b"PK"
    back = ZipCodec().decode(blob)
    assert len(back) == 3
    assert back.fps == 15
    assert back.frame_at(2).payload == seq.frame_at(2).payload


def test_zip_codec_rejects_bad_input() -> None:
    """ZipCodec 对空序列与非 zip 字节流都抛 CodecError。"""
    with pytest.raises(CodecError):
        ZipCodec().encode(FrameSequence())
    with pytest.raises(CodecError):
        ZipCodec().decode(b"not a zip container")


def test_timeline_duration_and_render() -> None:
    """Timeline 时长取各片段结束时刻的最大值，render 按起始时刻串接帧。"""
    timeline = Timeline()
    assert timeline.duration_ms() == 0
    first = _sequence(2)
    second = _sequence(3)
    assert timeline.add_clip(first, start_ms=0) == 0
    assert timeline.add_clip(second, start_ms=500) == 1
    assert timeline.duration_ms() == 500 + second.duration_ms()
    merged = timeline.render()
    assert len(merged) == 5
    assert merged.frame_at(0).payload == first.frame_at(0).payload
    assert merged.frame_at(4).payload == second.frame_at(2).payload


def test_render_canvas_to_frames_tags_each_frame() -> None:
    """render_canvas_to_frames 为每帧打上不同帧号；帧数非正时报错。"""
    canvas = Canvas(16, 16)
    canvas.add(rect(Rect(1, 1, 10, 10)))
    seq = render_canvas_to_frames(canvas, frames=4, fps=8)
    assert len(seq) == 4
    assert seq.fps == 8
    for i in range(4):
        assert f'data-frame="{i}"' in seq.frame_at(i).payload
    with pytest.raises(DrawvidError):
        render_canvas_to_frames(canvas, frames=0)


def test_build_video_writes_readable_zip(tmp_path) -> None:
    """build_video 写出可被 ZipCodec 解回的 zip 文件，并返回路径；空序列报错。"""
    seq = _sequence(4)
    out_path = tmp_path / "out.zip"
    assert build_video(seq, str(out_path)) == str(out_path)
    assert out_path.exists()
    blob = out_path.read_bytes()
    assert zipfile.ZipFile(io.BytesIO(blob)).namelist()
    assert len(ZipCodec().decode(blob)) == 4
    with pytest.raises(DrawvidError):
        build_video(FrameSequence(), str(tmp_path / "empty.zip"))
