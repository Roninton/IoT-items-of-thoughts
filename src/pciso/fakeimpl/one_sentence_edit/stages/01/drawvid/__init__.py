"""drawvid —— 一句话成片：纯标准库、零外部进程的参考实现。

本包只在这里重导出 core 层的公开符号；其它子模块由调用方按需直接 import，
这样早期阶段（assets / planner / render 尚未落盘时）本包仍然可导入。
"""

from .core import AudioEvent, CaptionCue, DrawvidError, Scene, Size, Timeline

__version__ = "0.1.0"
__all__ = [
    "AudioEvent",
    "CaptionCue",
    "DrawvidError",
    "Scene",
    "Size",
    "Timeline",
    "__version__",
]
