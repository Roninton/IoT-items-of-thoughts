"""冻结一致性测试 —— 行为契约，只读。

不要修改、删除、重命名或跳过本文件：每轮验证前 server 会从 P 恢复它，
改动无效且会被记为硬约束违反（C10）。

**刻意不用 `pytest.importorskip`。** 缺模块就是收集期错误 = 红。
用 importorskip 会让"没实现"变成"跳过"，而跳过在 pytest 里算通过 —— 那是个自我豁免的口子。

这里断言的是 P（PROJECT.md §6/§7/§8/§9）写死的行为，不是实现细节。
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import pytest
from drawvid.assets import Image, load_png, new_image, resize, write_png
from drawvid.audio import synthesize_bgm
from drawvid.container import encode_apng, write_player, write_vtt
from drawvid.core import AudioEvent, CaptionCue, DrawvidError, Scene, Size, Timeline
from drawvid.pipeline import make_video
from drawvid.planner import plan_from_sentence
from drawvid.render import render_frame, render_frames

# 小尺寸画布：冻结测试要快，尺寸不改变任何被断言的行为
W, H = 64, 36
BAND_Y = (int(H * 0.82) + int(H * 0.90)) // 2
BAND_X = W // 2


# --------------------------------------------------------------------------- #
# 契约符号必须真的能从 P 指定的模块 import 到
# --------------------------------------------------------------------------- #


def test_contract_symbols_are_importable_from_their_modules() -> None:
    """契约不只要求符号存在，还要求能从 P 指定的模块取到 —— 上面这些 import 就是断言。"""
    for symbol in (Size, Scene, CaptionCue, AudioEvent, Timeline, DrawvidError, Image):
        assert isinstance(symbol, type), f"{symbol!r} 不是类"
    for fn in (
        new_image,
        load_png,
        write_png,
        resize,
        plan_from_sentence,
        render_frame,
        render_frames,
        encode_apng,
        write_vtt,
        write_player,
        synthesize_bgm,
        make_video,
    ):
        assert callable(fn), f"{fn!r} 不可调用"


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """按 PNG 结构逐块走一遍；顺带校验每块的 CRC。

    不用 `data.count(b"fcTL")` 那种做法 —— 压缩数据里可能碰巧出现同样的字节。
    """
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "不是合法 PNG 签名"
    offset = 8
    chunks: list[tuple[bytes, bytes]] = []
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        ctype = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        (crc,) = struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])
        assert crc == zlib.crc32(ctype + payload) & 0xFFFFFFFF, f"{ctype!r} chunk CRC 不对"
        chunks.append((ctype, payload))
        offset += 12 + length
    assert chunks and chunks[-1][0] == b"IEND", "缺少 IEND"
    return chunks


def one_scene_timeline(duration: int = 6000, captions: list | None = None) -> Timeline:
    return Timeline(
        fps=30,
        size=Size(W, H),
        scenes=[Scene(asset_path="", start_ms=0, duration_ms=duration, effect="static")],
        captions=list(captions or []),
        events=[],
        title="",
    )


SENTENCE = "「演示」把素材做成 6 秒的片子，节奏放慢。"


# --------------------------------------------------------------------------- #
# core：时间轴
# --------------------------------------------------------------------------- #


def test_timeline_duration_and_frame_count() -> None:
    tl = one_scene_timeline(6000)
    assert tl.duration_ms() == 6000
    assert tl.frame_count() == 180  # 6000ms * 30fps / 1000


def test_timeline_frame_count_rounds_up() -> None:
    tl = one_scene_timeline(100)
    assert tl.frame_count() == 3  # 100ms * 30fps / 1000 = 3.0


def test_empty_timeline_is_zero_length() -> None:
    tl = Timeline(fps=30, size=Size(W, H), scenes=[], captions=[], events=[], title="")
    assert tl.duration_ms() == 0
    assert tl.frame_count() == 0


def test_timeline_to_json_is_stable_and_complete() -> None:
    tl = one_scene_timeline(6000, [CaptionCue(0, 3000, "甲")])
    a, b = tl.to_json(), tl.to_json()
    assert a == b, "同一 Timeline 两次序列化必须逐字相同"
    data = json.loads(a)
    assert data["fps"] == 30
    assert len(data["scenes"]) == 1
    assert len(data["captions"]) == 1


# --------------------------------------------------------------------------- #
# planner：确定性规则
# --------------------------------------------------------------------------- #


def test_planner_honours_explicit_duration() -> None:
    tl = plan_from_sentence(SENTENCE, ["a.png"])
    assert tl.duration_ms() == 6000


def test_planner_defaults_to_twelve_seconds() -> None:
    tl = plan_from_sentence("随便做一支片子", ["a.png"])
    assert tl.duration_ms() == 12000


def test_planner_is_deterministic() -> None:
    first = plan_from_sentence(SENTENCE, ["b.png", "a.png"])
    second = plan_from_sentence(SENTENCE, ["b.png", "a.png"])
    assert first.to_json() == second.to_json()


def test_planner_orders_assets_by_basename() -> None:
    tl = plan_from_sentence(SENTENCE, ["z/b.png", "a/a.png"])
    assert [s.asset_path for s in tl.scenes] == ["a/a.png", "z/b.png"]


def test_planner_uses_every_asset_exactly_once() -> None:
    tl = plan_from_sentence(SENTENCE, ["c.png", "a.png", "b.png"])
    assert len(tl.scenes) == 3
    assert sorted(s.asset_path for s in tl.scenes) == ["a.png", "b.png", "c.png"]


def test_planner_scenes_are_contiguous_and_cover_the_duration() -> None:
    tl = plan_from_sentence(SENTENCE, ["c.png", "a.png", "b.png"])
    assert tl.scenes[0].start_ms == 0
    for prev, cur in zip(tl.scenes, tl.scenes[1:], strict=False):
        assert cur.start_ms == prev.start_ms + prev.duration_ms
    assert tl.scenes[-1].start_ms + tl.scenes[-1].duration_ms == tl.duration_ms()


def test_planner_assigns_alternating_effects() -> None:
    tl = plan_from_sentence(SENTENCE, ["a.png", "b.png", "c.png"])
    assert [s.effect for s in tl.scenes] == ["kenburns_in", "kenburns_out", "kenburns_in"]


def test_planner_single_scene_is_static() -> None:
    tl = plan_from_sentence(SENTENCE, ["a.png"])
    assert tl.scenes[0].effect == "static"


def test_planner_without_assets_makes_one_graphic_scene() -> None:
    tl = plan_from_sentence(SENTENCE, [])
    assert len(tl.scenes) == 1
    assert tl.scenes[0].asset_path == ""
    assert tl.scenes[0].effect == "static"


def test_planner_splits_captions_on_punctuation() -> None:
    tl = plan_from_sentence(SENTENCE, ["a.png"])
    assert [c.text for c in tl.captions] == ["「演示」把素材做成 6 秒的片子", "节奏放慢"]


def test_planner_captions_tile_the_timeline() -> None:
    tl = plan_from_sentence(SENTENCE, ["a.png"])
    assert tl.captions[0].start_ms == 0
    assert tl.captions[-1].end_ms == tl.duration_ms()
    for prev, cur in zip(tl.captions, tl.captions[1:], strict=False):
        assert cur.start_ms == prev.end_ms, "字幕必须首尾相接，不留空档也不重叠"
        assert cur.end_ms > cur.start_ms


def test_planner_extracts_title_from_corner_brackets() -> None:
    assert plan_from_sentence(SENTENCE, ["a.png"]).title == "演示"


def test_planner_title_is_empty_without_brackets() -> None:
    assert plan_from_sentence("做一支片子", ["a.png"]).title == ""


def test_planner_emits_audio_events() -> None:
    tl = plan_from_sentence(SENTENCE, ["a.png", "b.png"])
    hits = [e for e in tl.events if e.kind == "hit"]
    risers = [e for e in tl.events if e.kind == "riser"]
    assert [e.t_ms for e in hits] == [0, 3000], "每个镜头起点一个 hit"
    assert [e.t_ms for e in risers] == [2500], "第 2 个镜头起点前 500ms 一个 riser"


def test_planner_uses_thirty_fps_and_small_canvas() -> None:
    tl = plan_from_sentence(SENTENCE, ["a.png"])
    assert tl.fps == 30
    assert tl.size.width == 640
    assert tl.size.height == 360


# --------------------------------------------------------------------------- #
# assets：纯标准库 PNG
# --------------------------------------------------------------------------- #


def test_new_image_and_get_pixel(tmp_path: Path) -> None:
    img = new_image(4, 3, (10, 20, 30, 255))
    assert (img.width, img.height) == (4, 3)
    assert img.get_pixel(0, 0) == (10, 20, 30, 255)
    assert img.get_pixel(3, 2) == (10, 20, 30, 255)


def test_png_roundtrip_preserves_every_pixel(tmp_path: Path) -> None:
    img = new_image(3, 2, (0, 0, 0, 0))
    wanted = {
        (0, 0): (255, 0, 0, 255),
        (1, 0): (0, 255, 0, 255),
        (2, 0): (0, 0, 255, 128),
        (0, 1): (7, 8, 9, 255),
        (1, 1): (200, 100, 50, 0),
        (2, 1): (255, 255, 255, 255),
    }
    for (x, y), rgba in wanted.items():
        img.put_pixel(x, y, rgba)
    path = tmp_path / "t.png"
    assert write_png(img, str(path)) == str(path)
    back = load_png(str(path))
    assert (back.width, back.height) == (3, 2)
    for (x, y), rgba in wanted.items():
        assert back.get_pixel(x, y) == rgba, f"({x},{y}) 像素没往返回来"


def test_written_png_is_structurally_valid(tmp_path: Path) -> None:
    path = tmp_path / "s.png"
    write_png(new_image(2, 2, (1, 2, 3, 4)), str(path))
    types = [t for t, _ in png_chunks(path.read_bytes())]
    assert types[0] == b"IHDR"
    assert b"IDAT" in types
    assert types[-1] == b"IEND"


def test_resize_changes_size_and_is_deterministic(tmp_path: Path) -> None:
    img = new_image(4, 4, (12, 34, 56, 255))
    big = resize(img, 8, 8)
    assert (big.width, big.height) == (8, 8)
    again = resize(img, 8, 8)
    assert [big.get_pixel(x, y) for x in range(8) for y in range(8)] == [
        again.get_pixel(x, y) for x in range(8) for y in range(8)
    ]


def test_load_png_rejects_non_png(tmp_path: Path) -> None:
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"this is not a png at all")
    with pytest.raises(DrawvidError):
        load_png(str(junk))


# --------------------------------------------------------------------------- #
# render：时间即画面
# --------------------------------------------------------------------------- #


def test_render_frame_is_deterministic() -> None:
    tl = one_scene_timeline(3000)
    a = render_frame(tl, 1234, {})
    b = render_frame(tl, 1234, {})
    assert [a.get_pixel(x, y) for x in range(0, W, 7) for y in range(0, H, 5)] == [
        b.get_pixel(x, y) for x in range(0, W, 7) for y in range(0, H, 5)
    ], "同一 t 两次渲染必须逐像素相同"


def test_render_frame_matches_timeline_size() -> None:
    img = render_frame(one_scene_timeline(1000), 0, {})
    assert (img.width, img.height) == (W, H)


def test_render_frames_count_matches_frame_count() -> None:
    tl = one_scene_timeline(1000)
    assert len(render_frames(tl, {})) == tl.frame_count() == 30


def test_background_is_never_pure_white() -> None:
    """背景通道被限定在 [16,200] —— 字幕带测试依赖这一点。"""
    tl = one_scene_timeline(3000)
    for t in (0, 500, 1500, 2999):
        img = render_frame(tl, t, {})
        for x in (0, W // 3, W // 2, W - 1):
            assert img.get_pixel(x, H // 2)[:3] != (255, 255, 255)


def test_caption_band_only_inside_cues() -> None:
    tl = one_scene_timeline(6000, [CaptionCue(1000, 2000, "有字幕")])
    inside = render_frame(tl, 1500, {})
    outside = render_frame(tl, 4000, {})
    assert inside.get_pixel(BAND_X, BAND_Y) == (255, 255, 255, 255)
    assert outside.get_pixel(BAND_X, BAND_Y)[:3] != (255, 255, 255)


def _progress_row(tl: Timeline, t: int) -> list[tuple[int, int, int, int]]:
    img = render_frame(tl, t, {})
    row = H - 2
    return [img.get_pixel(x, row) for x in range(W)]


def test_progress_bar_is_linear_in_time() -> None:
    """进度条必须**线性**正比于 t（P §8：已完成宽度 = W * t // duration）。

    这条断言原先写成"t=75% 时最右侧像素已经变白"，那等价于要求进度条在 75% 就接近满格。
    它和 P 的字面规格互相矛盾，并且会把实现逼去做加速缓动 —— 一旦发现这种矛盾，
    要改的是断言，不是规格。现在直接量填充边界，容差 ±2 像素。
    """
    tl = one_scene_timeline(6000)
    white = (255, 255, 255, 255)

    quarter = _progress_row(tl, 1500)  # 25% -> 填 W/4
    assert quarter[W // 4 - 1] == white, "25% 处应已填充"
    assert quarter[W // 4 + 2][:3] != (255, 255, 255), "25% 处不应填过头"

    three_quarter = _progress_row(tl, 4500)  # 75% -> 填 3W/4
    assert three_quarter[3 * W // 4 - 1] == white, "75% 处应已填充"
    assert three_quarter[3 * W // 4 + 2][:3] != (255, 255, 255), "75% 处不应填过头"


def test_progress_bar_is_monotonic() -> None:
    """时间往前走，进度条只能变长不能变短。"""
    tl = one_scene_timeline(6000)
    row = H - 2
    counts: list[int] = []
    for t in range(0, 6000, 500):
        img = render_frame(tl, t, {})
        counts.append(
            sum(1 for x in range(W) if img.get_pixel(x, row) == (255, 255, 255, 255))
        )
    assert counts == sorted(counts), f"进度条不单调: {counts}"
    assert counts[0] < counts[-1], "整段片子跑完，进度条总得动"


# --------------------------------------------------------------------------- #
# container：APNG / WebVTT / 播放页
# --------------------------------------------------------------------------- #


def test_apng_is_a_real_animation(tmp_path: Path) -> None:
    frames = [new_image(4, 4, (10, 10, 10, 255)), new_image(4, 4, (20, 20, 20, 255))]
    out = tmp_path / "m.apng"
    assert encode_apng(frames, str(out), fps=30) == str(out)
    chunks = png_chunks(out.read_bytes())
    types = [t for t, _ in chunks]
    assert b"acTL" in types, "APNG 必须有动画控制块 acTL"
    assert types.count(b"fcTL") == 2, "每帧一个 fcTL"
    assert b"fdAT" in types, "第 2 帧起数据在 fdAT 里"
    actl = next(p for t, p in chunks if t == b"acTL")
    assert struct.unpack(">II", actl) == (2, 0), "acTL 记录帧数与循环次数"


def test_apng_rejects_empty_frames(tmp_path: Path) -> None:
    with pytest.raises(DrawvidError):
        encode_apng([], str(tmp_path / "x.apng"))


def test_vtt_matches_the_timeline(tmp_path: Path) -> None:
    tl = one_scene_timeline(6000, [CaptionCue(0, 3000, "甲"), CaptionCue(3000, 6000, "乙")])
    out = tmp_path / "s.vtt"
    assert write_vtt(tl, str(out)) == str(out)
    body = out.read_text(encoding="utf-8")
    assert body.startswith("WEBVTT")
    assert body.count("-->") == len(tl.captions)
    assert "00:00:00.000 --> 00:00:03.000" in body
    assert "00:00:03.000 --> 00:00:06.000" in body
    assert "甲" in body and "乙" in body


def test_player_html_references_every_output(tmp_path: Path) -> None:
    tl = one_scene_timeline(1000)
    out = tmp_path / "index.html"
    assert write_player(tl, str(tmp_path)) == str(out)
    html = out.read_text(encoding="utf-8")
    assert "subs.vtt" in html, "播放页必须挂字幕轨"
    assert "bgm.wav" in html, "播放页必须挂音频"
    assert "frame_" in html, "播放页必须按帧播放"
    assert "<html" in html.lower()


# --------------------------------------------------------------------------- #
# audio：按时间轴合成 BGM
# --------------------------------------------------------------------------- #


def test_bgm_is_a_wav_with_the_timeline_length(tmp_path: Path) -> None:
    import wave

    tl = one_scene_timeline(1000)
    out = tmp_path / "b.wav"
    assert synthesize_bgm(tl, str(out)) == str(out)
    with wave.open(str(out), "rb") as fh:
        assert fh.getnchannels() == 1
        assert fh.getsampwidth() == 2
        assert fh.getnframes() == fh.getframerate() * 1000 // 1000, "采样数必须对齐时间轴"


def test_bgm_is_deterministic(tmp_path: Path) -> None:
    tl = one_scene_timeline(500)
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    synthesize_bgm(tl, str(a))
    synthesize_bgm(tl, str(b))
    assert a.read_bytes() == b.read_bytes()


# --------------------------------------------------------------------------- #
# pipeline：一句话进，五路输出出
# --------------------------------------------------------------------------- #


def test_make_video_produces_all_outputs(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    write_png(new_image(8, 8, (30, 60, 90, 255)), str(assets / "a.png"))
    write_png(new_image(8, 8, (90, 60, 30, 255)), str(assets / "b.png"))
    out_dir = tmp_path / "out"

    apng = make_video(SENTENCE, str(assets), str(out_dir))
    for name in ("movie.apng", "subs.vtt", "bgm.wav", "plan.json", "index.html"):
        assert (out_dir / name).is_file(), f"缺产物 {name}"
    assert Path(apng) == out_dir / "movie.apng"
    assert (out_dir / "frames").is_dir(), "播放页需要帧序列"


def test_outputs_are_mutually_aligned(tmp_path: Path) -> None:
    """多路输出必须都来自同一个时间轴 —— 这是本项目最重要的一条可测性质。"""
    import wave

    assets = tmp_path / "assets"
    assets.mkdir()
    write_png(new_image(8, 8, (30, 60, 90, 255)), str(assets / "a.png"))
    out_dir = tmp_path / "out"
    make_video(SENTENCE, str(assets), str(out_dir))

    plan = json.loads((out_dir / "plan.json").read_text(encoding="utf-8"))
    duration = sum(s["duration_ms"] for s in plan["scenes"])
    fps = plan["fps"]
    expected_frames = -(-duration * fps // 1000)  # 向上取整

    types = [t for t, _ in png_chunks((out_dir / "movie.apng").read_bytes())]
    assert types.count(b"fcTL") == expected_frames, "APNG 帧数必须等于 frame_count()"

    vtt = (out_dir / "subs.vtt").read_text(encoding="utf-8")
    assert vtt.count("-->") == len(plan["captions"]), "字幕条数必须等于时间轴上的 cue 数"

    with wave.open(str(out_dir / "bgm.wav"), "rb") as fh:
        expected_samples = duration * fh.getframerate() // 1000
        assert fh.getnframes() == expected_samples, "BGM 采样数必须对齐时间轴时长"

    frames_on_disk = sorted((out_dir / "frames").glob("frame_*.png"))
    assert len(frames_on_disk) == expected_frames, "落盘的帧数必须等于 frame_count()"
