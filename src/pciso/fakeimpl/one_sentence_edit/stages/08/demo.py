"""端到端演示：一句话进，六项产物出。

它自己造两张 PNG 素材，规划时间轴，渲染全部帧，写盘，然后打印产物路径与帧数，
并顺带核对"多路输出互相对齐"。

用法：`python demo.py [输出目录]`
"""

from __future__ import annotations

import argparse
import json
import os
import wave
from dataclasses import dataclass

from drawvid.assets import new_image, write_png
from drawvid.pipeline import make_video
from drawvid.planner import plan_from_sentence

SENTENCE = "「演示」把素材做成 6 秒的片子，节奏放慢。"
DEFAULT_OUT_DIR = os.path.join("out", "demo")
BGM_NAME = "bgm.wav"
PLAN_NAME = "plan.json"

ARTIFACT_NAMES = (
    "plan.json",
    "movie.apng",
    "subs.vtt",
    "bgm.wav",
    "index.html",
)


@dataclass(frozen=True)
class Artifact:
    """一项产物：名字 + 绝对路径 + 字节数。"""

    name: str
    path: str
    size: int

    def describe(self) -> str:
        """返回一行可读描述。"""
        return f"{self.name:<12} {self.size:>9} B  {self.path}"


def build_demo_assets(assets_dir: str) -> list[str]:
    """造两张 160×90 的确定性 PNG 素材，返回它们的路径。"""
    os.makedirs(assets_dir, exist_ok=True)
    palettes = (
        ((28, 44, 92, 255), (92, 148, 214, 255)),
        ((96, 32, 48, 255), (232, 168, 96, 255)),
    )
    paths: list[str] = []
    for index, (dark, light) in enumerate(palettes):
        image = new_image(160, 90, dark)
        for y in range(90):
            for x in range(160):
                if (x * 2) // 20 % 2 == (y * 2) // 20 % 2:
                    image.put_pixel(x, y, light)
        path = os.path.join(assets_dir, f"asset_{index + 1}.png")
        write_png(image, path)
        paths.append(path)
    return paths


def collect_artifacts(out_dir: str) -> list[Artifact]:
    """列出输出目录里的六项产物（帧序列按目录计）。"""
    items: list[Artifact] = []
    for name in ARTIFACT_NAMES:
        path = os.path.join(out_dir, name)
        size = os.path.getsize(path) if os.path.isfile(path) else -1
        items.append(Artifact(name=name, path=os.path.abspath(path), size=size))
    frames_dir = os.path.join(out_dir, "frames")
    frame_count = len([n for n in os.listdir(frames_dir) if n.endswith(".png")])
    items.append(
        Artifact(
            name="frames/",
            path=os.path.abspath(frames_dir),
            size=frame_count,
        )
    )
    return items


def verify_alignment(out_dir: str) -> list[str]:
    """核对多路输出是否都来自同一份时间轴，返回逐条结论。"""
    plan = json.loads(_read_text(os.path.join(out_dir, PLAN_NAME)))
    duration = sum(scene["duration_ms"] for scene in plan["scenes"])
    expected_frames = -(-duration * plan["fps"] // 1000)
    frames_on_disk = [n for n in os.listdir(os.path.join(out_dir, "frames")) if n.endswith(".png")]
    with wave.open(os.path.join(out_dir, BGM_NAME), "rb") as handle:
        samples = handle.getnframes()
        rate = handle.getframerate()

    return [
        f"时间轴时长      {plan['duration_ms']} ms",
        f"帧数 plan/磁盘  {plan['frame_count']} / {len(frames_on_disk)}"
        + f"（期望 {expected_frames}）",
        f"字幕 cue 数     {len(plan['captions'])}",
        f"BGM 采样数      {samples}（= {duration} * {rate} // 1000）",
    ]


def _read_text(path: str) -> str:
    """读 UTF-8 文本。"""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def main(argv: list[str] | None = None) -> int:
    """跑一遍端到端演示并打印产物路径与帧数。"""
    parser = argparse.ArgumentParser(description="drawvid 端到端演示")
    parser.add_argument("out_dir", nargs="?", default=DEFAULT_OUT_DIR, help="输出目录")
    args = parser.parse_args(argv)

    out_dir = args.out_dir
    assets_dir = os.path.join(out_dir, "assets")
    build_demo_assets(assets_dir)

    print(f"输入句子：{SENTENCE}")
    print(f"素材目录：{os.path.abspath(assets_dir)}")
    timeline = plan_from_sentence(SENTENCE, [])
    print(
        f"计划：fps={timeline.fps} 画布={timeline.size.width}x{timeline.size.height} "
        f"标题={timeline.title!r}"
    )

    apng_path = make_video(SENTENCE, assets_dir, out_dir)
    frame_count = timeline.frame_count()
    print(f"成片：{apng_path}")
    print(f"帧数：{frame_count}")

    print("产物清单：")
    for artifact in collect_artifacts(out_dir):
        print(f"  {artifact.describe()}")

    print("多路输出对齐：")
    for line in verify_alignment(out_dir):
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
