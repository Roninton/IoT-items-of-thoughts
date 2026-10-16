"""drawvid 端到端 demo：画布 → 帧序列 → zip 视频 → 解码校验。"""

from __future__ import annotations

import os
import tempfile

from drawvid.core import Rect
from drawvid.drawing import Canvas, rect
from drawvid.pipeline import build_video, render_canvas_to_frames
from drawvid.video import ZipCodec


def main() -> None:
    """跑通一条最小链路并打印帧数、时长、解码帧数与容器字节数。"""
    canvas = Canvas(64, 64)
    canvas.add(rect(Rect(8, 8, 48, 48)))

    sequence = render_canvas_to_frames(canvas, frames=4, fps=12)

    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = os.path.join(tmp_dir, "drawvid.zip")
        build_video(sequence, out_path)
        with open(out_path, "rb") as handle:
            blob = handle.read()

    decoded = ZipCodec().decode(blob)
    print(
        f"frames={len(sequence)} duration_ms={sequence.duration_ms()} "
        f"decoded={len(decoded)} bytes={len(blob)}"
    )


if __name__ == "__main__":
    main()
