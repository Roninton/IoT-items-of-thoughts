"""容器层：APNG、WebVTT 与播放页。

本模块只依赖 core 与 assets；**不得** import render / planner / audio ——
它只接收现成的帧与 Timeline，不关心画面是怎么算出来的，也不关心声音。

产物是**真 APNG**：`acTL` 记录帧数与循环次数，第 1 帧数据在 `IDAT`，
第 2 帧起在 `fdAT`，每帧各一个 `fcTL`，每个块的 CRC 都由 zlib.crc32 算出。
"""

from __future__ import annotations

import os
import struct
import zlib

from .assets import Image
from .core import DrawvidError, Timeline

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
FILTER_NONE = 0
APNG_DISPOSE_NONE = 0
APNG_BLEND_SOURCE = 0
APNG_LOOP_FOREVER = 0
PNG_DEPTH_8 = 8
PNG_COLOR_RGBA = 6
FRAME_NAME_PATTERN = "frame_%04d.png"
FRAMES_DIR_NAME = "frames"
VTT_NAME = "subs.vtt"
BGM_NAME = "bgm.wav"
APNG_NAME = "movie.apng"
PLAYER_NAME = "index.html"


def _chunk(ctype: bytes, payload: bytes) -> bytes:
    """组装一个 PNG 块：长度 + 类型 + 数据 + CRC。"""
    return (
        struct.pack(">I", len(payload))
        + ctype
        + payload
        + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF)
    )


def _encode_rgba(image: Image) -> bytes:
    """把 Image 编码成逐行 filter 0 的 zlib 压缩数据。"""
    filtered = bytearray()
    for y in range(image.height):
        filtered.append(FILTER_NONE)
        filtered.extend(image._row_bytes(y))
    return zlib.compress(bytes(filtered), 9)


def _ensure_parent(path: str) -> None:
    """确保目标文件的父目录存在。"""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)


def _write_bytes(path: str, data: bytes) -> str:
    """写文件（自动建父目录），返回路径。"""
    _ensure_parent(path)
    try:
        with open(path, "wb") as handle:
            handle.write(data)
    except OSError as exc:
        raise DrawvidError(f"无法写入文件：{path}（{exc}）") from exc
    return path


def _write_text(path: str, text: str) -> str:
    """写 UTF-8 文本文件（自动建父目录），返回路径。"""
    _ensure_parent(path)
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except OSError as exc:
        raise DrawvidError(f"无法写入文件：{path}（{exc}）") from exc
    return path


def encode_apng(frames: list[Image], out_path: str, fps: int = 30) -> str:
    """把帧序列封装成真 APNG（acTL / fcTL / IDAT / fdAT），返回路径。"""
    if not frames:
        raise DrawvidError("encode_apng 需要至少一帧")
    if fps <= 0:
        raise DrawvidError(f"非法帧率 {fps}")
    first = frames[0]
    delay_num = 1000
    delay_den = fps * 1000

    payload = bytearray(PNG_SIGNATURE)
    for later in frames[1:]:
        if (later.width, later.height) != (first.width, first.height):
            raise DrawvidError("APNG 的所有帧必须尺寸一致")
    ihdr = struct.pack(
        ">IIBBBBB", first.width, first.height, PNG_DEPTH_8, PNG_COLOR_RGBA, 0, 0, 0
    )
    payload.extend(_chunk(b"IHDR", ihdr))
    payload.extend(_chunk(b"acTL", struct.pack(">II", len(frames), APNG_LOOP_FOREVER)))

    sequence = 0
    for index, frame in enumerate(frames):
        payload.extend(
            _chunk(
                b"fcTL",
                struct.pack(
                    ">IIIIIHHBB",
                    sequence,
                    frame.width,
                    frame.height,
                    0,
                    0,
                    delay_num,
                    delay_den,
                    APNG_DISPOSE_NONE,
                    APNG_BLEND_SOURCE,
                ),
            )
        )
        sequence += 1
        data = _encode_rgba(frame)
        if index == 0:
            payload.extend(_chunk(b"IDAT", data))
        else:
            payload.extend(_chunk(b"fdAT", struct.pack(">I", sequence) + data))
            sequence += 1

    payload.extend(_chunk(b"IEND", b""))
    return _write_bytes(out_path, bytes(payload))


def _format_timestamp(ms: int) -> str:
    """把毫秒格式化成 WebVTT 的 hh:mm:ss.mmm。"""
    total = max(0, int(ms))
    hours, rest = divmod(total, 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def write_vtt(timeline: Timeline, out_path: str) -> str:
    """按时间轴写出 WebVTT 字幕文件，返回路径。"""
    lines = ["WEBVTT", ""]
    for index, cue in enumerate(timeline.captions, start=1):
        lines.append(str(index))
        lines.append(f"{_format_timestamp(cue.start_ms)} --> {_format_timestamp(cue.end_ms)}")
        lines.append(cue.text)
        lines.append("")
    return _write_text(out_path, "\n".join(lines))


def write_player(timeline: Timeline, out_dir: str) -> str:
    """在 `out_dir/index.html` 生成播放页：逐帧画面 + `<track>` 字幕 + `<audio>`。"""
    fps = max(1, timeline.fps)
    frame_total = timeline.frame_count()
    duration_ms = timeline.duration_ms()
    title = timeline.title or "drawvid"
    html = _player_html(title, fps, frame_total, duration_ms)
    return _write_text(os.path.join(out_dir, PLAYER_NAME), html)


def _player_html(title: str, fps: int, frame_total: int, duration_ms: int) -> str:
    """拼播放页 HTML：画面、字幕、音频三路按同一个 fps 对齐。"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ background: #101014; color: #e8e8ee; font-family: sans-serif; text-align: center; }}
  #stage {{ max-width: 100%; image-rendering: pixelated; }}
  video, img {{ margin: 12px auto; display: block; }}
</style>
</head>
<body>
<h1>{title}</h1>
<img id="stage" width="{fps}" alt="{title}">
<audio id="bgm" src="{BGM_NAME}" controls></audio>
<video id="subs-host" width="0" height="0" muted>
  <track kind="subtitles" srclang="zh" label="字幕" src="{VTT_NAME}" default>
</video>
<script>
// 画面是时间的函数：按 fps 步进帧序列，与 bgm.wav、subs.vtt 共用同一条时间轴。
const FPS = {fps};
const FRAME_TOTAL = {frame_total};
const DURATION_MS = {duration_ms};
const PATTERN = "{FRAMES_DIR_NAME}/{FRAME_NAME_PATTERN}";
const stage = document.getElementById("stage");
const bgm = document.getElementById("bgm");
let frameIndex = -1;
function framePath(index) {{
  const n = String(index).padStart(4, "0");
  return PATTERN.replace("%04d", n);
}}
function showFrame(index) {{
  if (index === frameIndex || index < 0 || index >= FRAME_TOTAL) return;
  frameIndex = index;
  stage.src = framePath(index);
}}
function tick() {{
  const ms = bgm.currentTime * 1000;
  showFrame(Math.min(FRAME_TOTAL - 1, Math.floor(ms * FPS / 1000)));
  window.requestAnimationFrame(tick);
}}
bgm.addEventListener("play", () => window.requestAnimationFrame(tick));
showFrame(0);
</script>
</body>
</html>
"""
