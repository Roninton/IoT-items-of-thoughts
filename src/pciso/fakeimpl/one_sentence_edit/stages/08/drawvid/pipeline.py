"""编排层：一句话进，多路输出出。

本模块可以依赖全部下层：把 `planner`（要什么）、`render`（怎么画）、
`assets`（素材）、`container`（封装）、`audio`（合成）串成一条链。

产物永远来自**同一个 Timeline**：清单里的帧数、落盘的帧数、APNG 的 `fcTL` 数、
字幕的 cue 数、BGM 的采样数，全都由那一份时间轴推导。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .assets import Image, load_png, write_png
from .audio import synthesize_bgm
from .container import encode_apng, write_player, write_vtt
from .core import Timeline
from .planner import plan_from_sentence
from .render import render_frames

PLAN_NAME = "plan.json"
FRAMES_DIR_NAME = "frames"
FRAME_NAME_PATTERN = "frame_%04d.png"
APNG_NAME = "movie.apng"
VTT_NAME = "subs.vtt"
BGM_NAME = "bgm.wav"
PLAYER_NAME = "index.html"
PNG_SUFFIX = ".png"


def _write_text(path: str, text: str) -> str:
    """写 UTF-8 文本（LF 换行），返回路径。"""
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return path


def _iter_asset_paths(assets_dir: str) -> list[str]:
    """列出素材目录里的 PNG 路径，按字典序排序（确定性）。"""
    if not os.path.isdir(assets_dir):
        return []
    entries = [
        os.path.join(assets_dir, name)
        for name in os.listdir(assets_dir)
        if name.lower().endswith(PNG_SUFFIX) and os.path.isfile(os.path.join(assets_dir, name))
    ]
    return sorted(entries)


def _load_asset_images(timeline: Timeline) -> dict[str, Image]:
    """按时间轴需要加载素材；读不动的素材直接跳过（该镜头只画背景）。"""
    images: dict[str, Image] = {}
    for scene in timeline.scenes:
        if not scene.asset_path or scene.asset_path in images:
            continue
        try:
            images[scene.asset_path] = load_png(scene.asset_path)
        except OSError:
            continue
    return images


def make_video(sentence: str, assets_dir: str, out_dir: str) -> str:
    """一句话 + 素材目录 -> 六项产物，返回 `movie.apng` 的路径。"""
    timeline = plan_from_sentence(sentence, _iter_asset_paths(assets_dir))
    frames_dir = os.path.join(out_dir, FRAMES_DIR_NAME)
    os.makedirs(frames_dir, exist_ok=True)

    frames = render_frames(timeline, _load_asset_images(timeline))
    for index, frame in enumerate(frames):
        write_png(frame, os.path.join(frames_dir, FRAME_NAME_PATTERN % index))

    _write_text(os.path.join(out_dir, PLAN_NAME), timeline.to_json())
    apng_path = encode_apng(frames, os.path.join(out_dir, APNG_NAME), fps=timeline.fps)
    write_vtt(timeline, os.path.join(out_dir, VTT_NAME))
    synthesize_bgm(timeline, os.path.join(out_dir, BGM_NAME))
    write_player(timeline, out_dir)
    return apng_path


def _frame_files_on_disk(out_dir: str) -> list[Path]:
    """返回输出目录里实际落盘的帧文件，供自检与测试使用。"""
    return sorted(Path(out_dir, FRAMES_DIR_NAME).glob("frame_*.png"))


def _main(argv: list[str] | None = None) -> int:
    """CLI：`python -m drawvid.pipeline "一句话" 素材目录 输出目录`。"""
    parser = argparse.ArgumentParser(description="一句话成片（纯标准库）")
    parser.add_argument("sentence", help="要成片的那句话")
    parser.add_argument("assets_dir", help="素材目录（PNG）")
    parser.add_argument("out_dir", help="输出目录")
    args = parser.parse_args(argv)

    apng_path = make_video(args.sentence, args.assets_dir, args.out_dir)
    print(f"成片：{apng_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
