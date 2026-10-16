"""冻结契约测试：drawvid 的 9 个契约符号的行为。

本文件会被复制到目标仓库的 ``tests/frozen/test_contract.py``，并以 ``PYTHONPATH=<仓库根>`` 运行。
它只断言对外可观察的行为，不触碰任何私有实现细节。

**所有契约模块必须在顶层 import。** 这样"模块还没实现"会表现为收集期错误（红），
而不是被 importorskip 之类的手段变成"跳过" —— 跳过在 pytest 里算通过，
那等于给工作模型留了一个自我豁免的口子。
"""

from __future__ import annotations

import io
import zipfile

from drawvid.core import Point, Rect
from drawvid.drawing import Canvas, line, rect
from drawvid.pipeline import build_video
from drawvid.video import Codec, Frame, FrameSequence, Timeline


def test_core_point_is_a_value_object() -> None:
    """Point 是可按值比较的坐标对象。"""

    assert Point(1, 2) == Point(1, 2)
    assert Point(1, 2) != Point(2, 1)


def test_core_rect_is_a_value_object() -> None:
    """Rect 是可按值比较的矩形对象。"""

    assert Rect(0, 0, 4, 3) == Rect(0, 0, 4, 3)
    assert Rect(0, 0, 4, 3) != Rect(0, 0, 4, 4)


def test_drawing_line_returns_point_sequence() -> None:
    """line 返回点列，端点即传入的坐标。"""
    from drawvid.drawing import line

    points = line(0, 0, 5, 0)
    assert points
    assert all(isinstance(p, Point) for p in points)
    assert points[0] == Point(0, 0)
    assert points[-1] == Point(5, 0)


def test_drawing_rect_returns_closed_outline() -> None:
    """rect 返回非空且不重复的点列。"""

    points = rect(Rect(0, 0, 3, 2))
    assert points
    assert all(isinstance(p, Point) for p in points)
    assert len(set(points)) == len(points)


def test_drawing_canvas_to_svg_returns_svg_text() -> None:
    """Canvas.to_svg 返回 SVG 文本。"""

    canvas = Canvas(16, 16)
    canvas.add(line(0, 0, 15, 15))
    svg = canvas.to_svg()
    assert isinstance(svg, str)
    assert svg.strip()


def test_video_frame_sequence_append_returns_index() -> None:
    """FrameSequence.append 返回新帧的位置索引。"""
    from drawvid.video import Frame, FrameSequence

    seq = FrameSequence()
    assert seq.append(Frame(index=0, payload="a")) == 0
    assert seq.append(Frame(index=1, payload="b")) == 1


def test_video_codec_encode_returns_bytes() -> None:
    """Codec.encode 在具体编解码器上返回字节流。"""
    from drawvid.video import Frame, FrameSequence

    class _IdentityCodec(Codec):
        """最小可用编解码器：直接把负载拼成字节。"""

        name = "identity"

        def encode(self, sequence: FrameSequence) -> bytes:
            """把帧负载依次编码。"""
            return b"".join(
                sequence.frame_at(i).payload.encode("utf-8") for i in range(len(sequence))
            )

        def decode(self, blob: bytes) -> FrameSequence:
            """把字节流还原成单帧序列。"""
            seq = FrameSequence()
            seq.append(Frame(index=0, payload=blob.decode("utf-8")))
            return seq

    seq = FrameSequence()
    seq.append(Frame(index=0, payload="abc"))
    blob = _IdentityCodec().encode(seq)
    assert isinstance(blob, bytes)
    assert blob == b"abc"


def test_video_timeline_duration_ms_is_non_negative() -> None:
    """Timeline.duration_ms 返回非负整数。"""

    timeline = Timeline()
    duration = timeline.duration_ms()
    assert isinstance(duration, int)
    assert duration == 0


def test_pipeline_build_video_writes_file(tmp_path) -> None:
    """build_video 把帧序列写成一个非空的 zip 文件，并返回该路径。"""
    seq = FrameSequence()
    seq.append(Frame(index=0, payload='<svg data-frame="0"></svg>'))
    seq.append(Frame(index=1, payload='<svg data-frame="1"></svg>'))
    out_path = tmp_path / "contract_video.zip"
    result = build_video(seq, str(out_path))
    assert result == str(out_path)
    assert out_path.is_file()
    blob = out_path.read_bytes()
    assert blob
    assert zipfile.ZipFile(io.BytesIO(blob)).namelist()
