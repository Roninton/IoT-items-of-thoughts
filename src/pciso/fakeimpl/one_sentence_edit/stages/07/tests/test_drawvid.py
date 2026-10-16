"""drawvid 自测套件。

冻结契约测试（`tests/frozen/`）管的是**接口行为**；本套件管的是**公开符号被真的走到**：
契约里的每个公开类 / 函数 / 方法在这里至少被调用一次，外加几条不变量回归。
"""

from __future__ import annotations

import json
import struct
import wave
import zlib
from pathlib import Path

import pytest

from drawvid.assets import Image, load_png, new_image, resize, write_png
from drawvid.audio import synthesize_bgm
from drawvid.container import encode_apng, write_player, write_vtt
from drawvid.core import AudioEvent, CaptionCue, CodecError, DrawvidError, Scene, Size, Timeline
from drawvid.pipeline import make_video
from drawvid.planner import plan_from_sentence
from drawvid.render import render_frame, render_frames

SENTENCE = "「演示」把素材做成 6 秒的片子，节奏放慢。"


def test_codec_error_is_a_drawvid_error(tmp_path: Path) -> None:
    """编解码失败抛 CodecError，且它是 DrawvidError 的子类。

    调用方因此可以只 catch 基类，也可以精细区分"解码失败"与"参数非法 / 文件不存在"。
    异常层次属于对外契约的一部分，必须有测试盯着。
    """
    assert issubclass(CodecError, DrawvidError)

    junk = tmp_path / "junk.png"
    junk.write_bytes(b"this file exists but is not a PNG at all")
    with pytest.raises(CodecError):
        load_png(str(junk))

    # 文件不存在是 I/O 问题，不该冒充编解码失败
    with pytest.raises(DrawvidError):
        load_png(str(tmp_path / "missing.png"))


def one_scene(duration_ms: int = 3000, captions: list[CaptionCue] | None = None) -> Timeline:
    """构造一条只有一个纯图形镜头的测试时间轴。"""
    return Timeline(
        fps=30,
        size=Size(64, 36),
        scenes=[Scene(asset_path="", start_ms=0, duration_ms=duration_ms, effect="static")],
        captions=list(captions or []),
        events=[AudioEvent(t_ms=0, kind="hit")],
        title="测试",
    )


def sample_png(tmp_path: Path, name: str = "src.png") -> str:
    """写一张有花纹的小 PNG，返回路径。"""
    image = new_image(4, 4, (0, 0, 0, 255))
    for y in range(4):
        for x in range(4):
            image.put_pixel(x, y, (x * 40, y * 40, 128, 255))
    return write_png(image, str(tmp_path / name))


# --------------------------------------------------------------------------- #
# core
# --------------------------------------------------------------------------- #


def test_core_types_and_attributes() -> None:
    """core 的四类数据模型字段可读。"""
    size = Size(64, 36)
    scene = Scene(asset_path="a.png", start_ms=0, duration_ms=1000, effect="static")
    cue = CaptionCue(start_ms=0, end_ms=500, text="甲")
    event = AudioEvent(t_ms=10, kind="hit")
    tl = Timeline(fps=30, size=size, scenes=[scene], captions=[cue], events=[event], title="t")
    assert (tl.fps, tl.title) == (30, "t")
    assert tl.scenes[0].asset_path == "a.png"
    assert tl.captions[0].text == "甲"
    assert tl.events[0].kind == "hit"


def test_timeline_duration_frames_and_json() -> None:
    """时长、帧数与 JSON 快照都自洽且稳定。"""
    tl = one_scene(1000)
    assert tl.duration_ms() == 1000
    assert tl.frame_count() == 30
    payload = json.loads(tl.to_json())
    assert payload["duration_ms"] == 1000
    assert payload["frame_count"] == 30
    assert payload["title"] == "测试"
    assert tl.to_json() == tl.to_json()


def test_empty_timeline_is_zero() -> None:
    """空时间轴的时长为 0、帧数为 0。"""
    empty = Timeline(fps=30, size=Size(8, 8), scenes=[], captions=[], events=[], title="")
    assert empty.duration_ms() == 0
    assert empty.frame_count() == 0


def test_drawvid_error_is_an_exception() -> None:
    """DrawvidError 是异常基类。"""
    assert issubclass(DrawvidError, Exception)
    with pytest.raises(DrawvidError):
        raise DrawvidError("炸了")


# --------------------------------------------------------------------------- #
# assets
# --------------------------------------------------------------------------- #


def test_image_pixel_accessors(tmp_path: Path) -> None:
    """Image 的宽高与读写像素，非法尺寸抛错。"""
    image = new_image(3, 2, (1, 2, 3, 4))
    assert isinstance(image, Image)
    assert (image.width, image.height) == (3, 2)
    image.put_pixel(1, 1, (250, 240, 230, 220))
    assert image.get_pixel(1, 1) == (250, 240, 230, 220)
    with pytest.raises(DrawvidError):
        new_image(0, 4, (0, 0, 0, 255))


def test_png_roundtrip_is_pixel_exact(tmp_path: Path) -> None:
    """write_png -> load_png 逐像素往返一致。"""
    path = sample_png(tmp_path)
    back = load_png(path)
    assert (back.width, back.height) == (4, 4)
    assert back.get_pixel(2, 3) == (80, 120, 128, 255)


def test_png_chunks_are_well_formed(tmp_path: Path) -> None:
    """写出的 PNG 块序与 CRC 都正确。"""
    data = Path(sample_png(tmp_path)).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    offset = 8
    types: list[bytes] = []
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        ctype = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        (crc,) = struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])
        assert crc == zlib.crc32(ctype + payload) & 0xFFFFFFFF
        types.append(ctype)
        offset += 12 + length
    assert types == [b"IHDR", b"IDAT", b"IEND"]


def test_load_png_rejects_garbage(tmp_path: Path) -> None:
    """非 PNG 字节流被拒。"""
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not a png")
    with pytest.raises(DrawvidError):
        load_png(str(bad))


def test_load_png_rejects_missing_file(tmp_path: Path) -> None:
    """文件不存在时抛 DrawvidError。"""
    with pytest.raises(DrawvidError):
        load_png(str(tmp_path / "nope.png"))


def test_resize_is_nearest_neighbour_and_deterministic() -> None:
    """最近邻缩放尺寸正确且可复现。"""
    image = new_image(2, 2, (10, 20, 30, 255))
    image.put_pixel(0, 0, (1, 1, 1, 255))
    big = resize(image, 4, 4)
    assert (big.width, big.height) == (4, 4)
    assert big.get_pixel(0, 0) == (1, 1, 1, 255)
    assert big.get_pixel(3, 3) == (10, 20, 30, 255)
    assert resize(image, 4, 4).get_pixel(2, 2) == big.get_pixel(2, 2)


# --------------------------------------------------------------------------- #
# planner
# --------------------------------------------------------------------------- #


def test_planner_full_rule_set() -> None:
    """九条规划规则一次跑通。"""
    tl = plan_from_sentence(SENTENCE, ["z/b.png", "a/a.png"])
    assert tl.duration_ms() == 6000
    assert tl.fps == 30
    assert (tl.size.width, tl.size.height) == (640, 360)
    assert [scene.asset_path for scene in tl.scenes] == ["a/a.png", "z/b.png"]
    assert [scene.effect for scene in tl.scenes] == ["kenburns_in", "kenburns_out"]
    assert [cue.text for cue in tl.captions] == ["「演示」把素材做成 6 秒的片子", "节奏放慢"]
    assert tl.title == "演示"
    assert [event.kind for event in tl.events] == ["hit", "hit", "riser"]


def test_planner_defaults_and_empty_assets() -> None:
    """默认时长与空素材场景。"""
    tl = plan_from_sentence("随便做一支片子", [])
    assert tl.duration_ms() == 12000
    assert len(tl.scenes) == 1
    assert tl.scenes[0].asset_path == ""
    assert tl.scenes[0].effect == "static"
    assert tl.title == ""


def test_planner_splits_long_segments_and_tiles() -> None:
    """超长段按 20 字硬切并铺满时间轴。"""
    tl = plan_from_sentence("甲" * 45, ["a.png"])
    assert [len(cue.text) for cue in tl.captions] == [20, 20, 5]
    assert tl.captions[0].start_ms == 0
    assert tl.captions[-1].end_ms == tl.duration_ms()


def test_planner_quoted_title() -> None:
    """双引号标题也能提取。"""
    assert plan_from_sentence('做一支 "标题甲" 的片子', ["a.png"]).title == "标题甲"


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #


def test_render_frame_background_progress_and_band() -> None:
    """背景、进度条与字幕带各就其位。"""
    tl = one_scene(3000, [CaptionCue(0, 1500, "有字")])
    inside = render_frame(tl, 100, {})
    outside = render_frame(tl, 2000, {})
    assert (inside.width, inside.height) == (64, 36)
    band_y = (int(36 * 0.82) + int(36 * 0.90)) // 2
    assert inside.get_pixel(32, band_y) == (255, 255, 255, 255)
    assert outside.get_pixel(32, band_y)[:3] != (255, 255, 255)
    assert inside.get_pixel(0, 0)[:3] != (255, 255, 255)


def gradient_png(tmp_path: Path, name: str = "grad.png") -> str:
    """写一张 64×64 的水平渐变 PNG，放大窗口变化时能看到像素差异。"""
    image = new_image(64, 64, (0, 0, 0, 255))
    for y in range(64):
        for x in range(64):
            image.put_pixel(x, y, (x * 4, y * 4, 128, 255))
    return write_png(image, str(tmp_path / name))


def test_render_frame_applies_kenburns_with_assets(tmp_path: Path) -> None:
    """Ken Burns 真的改变画面，static 不变。"""
    path = gradient_png(tmp_path)
    source = load_png(path)
    size = Size(8, 6)

    def frame_at(effect: str, t_ms: int) -> Image:
        """按效果与时刻渲染一帧。"""
        timeline = Timeline(
            fps=30,
            size=size,
            scenes=[Scene(asset_path=path, start_ms=0, duration_ms=1000, effect=effect)],
            captions=[],
            events=[],
            title="",
        )
        return render_frame(timeline, t_ms, {path: source})

    def upper_rows(image: Image) -> bytes:
        """取掐掉底部进度条后的像素字节。"""
        # 掐掉底部进度条：它本来就随时间变化，不是 Ken Burns 的证据
        stride = size.width * 4 * (size.height - 4)
        return bytes(image._data)[:stride]

    assert upper_rows(frame_at("kenburns_in", 0)) != upper_rows(frame_at("kenburns_in", 999))
    assert upper_rows(frame_at("kenburns_out", 0)) != upper_rows(frame_at("kenburns_out", 999))
    assert upper_rows(frame_at("static", 0)) == upper_rows(frame_at("static", 999))


def test_render_frame_handles_empty_timeline() -> None:
    """空时间轴也能渲染出一帧。"""
    empty = Timeline(fps=30, size=Size(8, 8), scenes=[], captions=[], events=[], title="")
    image = render_frame(empty, 500, {})
    assert (image.width, image.height) == (8, 8)


def test_render_frames_count_and_placement() -> None:
    """帧数与取样时刻对齐。"""
    tl = one_scene(1000)
    frames = render_frames(tl, {})
    assert len(frames) == tl.frame_count() == 30
    assert frames[0].get_pixel(0, 0) == render_frame(tl, 0, {}).get_pixel(0, 0)


# --------------------------------------------------------------------------- #
# container
# --------------------------------------------------------------------------- #


def test_apng_structure(tmp_path: Path) -> None:
    """APNG 的 fcTL / fdAT / acTL 结构正确，空帧被拒。"""
    frames = [new_image(4, 4, (i * 10, 0, 0, 255)) for i in range(3)]
    out = tmp_path / "m.apng"
    assert encode_apng(frames, str(out), fps=24) == str(out)
    data = out.read_bytes()
    assert data.count(b"fcTL") == 3
    assert b"fdAT" in data
    assert struct.unpack(">II", data[data.index(b"acTL") + 4 : data.index(b"acTL") + 12]) == (3, 0)
    with pytest.raises(DrawvidError):
        encode_apng([], str(tmp_path / "x.apng"))


def test_vtt_layout(tmp_path: Path) -> None:
    """WebVTT 头部与时间戳格式正确。"""
    tl = one_scene(2000, [CaptionCue(0, 1000, "甲"), CaptionCue(1000, 2000, "乙")])
    out = tmp_path / "s.vtt"
    assert write_vtt(tl, str(out)) == str(out)
    body = out.read_text(encoding="utf-8")
    assert body.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:01.000" in body
    assert body.count("-->") == 2


def test_player_references_all_outputs(tmp_path: Path) -> None:
    """播放页引用了帧序列、字幕与音频。"""
    html_path = write_player(one_scene(1000), str(tmp_path))
    html = Path(html_path).read_text(encoding="utf-8")
    assert html_path.endswith("index.html")
    assert "subs.vtt" in html
    assert "bgm.wav" in html
    assert "frame_%04d.png" in html


# --------------------------------------------------------------------------- #
# audio
# --------------------------------------------------------------------------- #


def test_bgm_format_and_length(tmp_path: Path) -> None:
    """BGM 的声道、位深、采样率与采样数正确。"""
    out = tmp_path / "b.wav"
    assert synthesize_bgm(one_scene(1500), str(out)) == str(out)
    with wave.open(str(out), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 22050
        assert handle.getnframes() == 1500 * 22050 // 1000


def test_bgm_is_byte_identical(tmp_path: Path) -> None:
    """同一时间轴两次合成的 WAV 逐字节相同。"""
    first, second = tmp_path / "a.wav", tmp_path / "b.wav"
    tl = plan_from_sentence(SENTENCE, ["a.png", "b.png"])
    synthesize_bgm(tl, str(first))
    synthesize_bgm(tl, str(second))
    assert first.read_bytes() == second.read_bytes()


# --------------------------------------------------------------------------- #
# pipeline
# --------------------------------------------------------------------------- #


def test_make_video_writes_every_artifact(tmp_path: Path) -> None:
    """六项产物齐全且帧数一致。"""
    assets = tmp_path / "assets"
    assets.mkdir()
    sample_png(assets, "a.png")
    sample_png(assets, "b.png")
    out_dir = tmp_path / "out"

    apng = make_video("「演示」把素材做成 2 秒的片子，节奏放慢。", str(assets), str(out_dir))
    assert Path(apng) == out_dir / "movie.apng"
    for name in ("plan.json", "movie.apng", "subs.vtt", "bgm.wav", "index.html"):
        assert (out_dir / name).is_file()
    plan = json.loads((out_dir / "plan.json").read_text(encoding="utf-8"))
    frames = sorted((out_dir / "frames").glob("frame_*.png"))
    assert len(frames) == plan["frame_count"]


def test_make_video_is_reproducible(tmp_path: Path) -> None:
    """两次 make_video 的 APNG 与 WAV 逐字节相同。"""
    assets = tmp_path / "assets"
    assets.mkdir()
    sample_png(assets, "a.png")
    first, second = tmp_path / "o1", tmp_path / "o2"
    make_video("「演示」2 秒", str(assets), str(first))
    make_video("「演示」2 秒", str(assets), str(second))
    assert (first / "movie.apng").read_bytes() == (second / "movie.apng").read_bytes()
    assert (first / "bgm.wav").read_bytes() == (second / "bgm.wav").read_bytes()
