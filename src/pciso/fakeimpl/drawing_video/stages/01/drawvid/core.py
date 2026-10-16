"""核心类型与错误。

本模块是依赖图的**叶子**：不得 import 任何其他 drawvid 子模块。
"""

from __future__ import annotations

from dataclasses import dataclass


class DrawvidError(Exception):
    """drawvid 全部异常的基类。"""


class CodecError(DrawvidError):
    """编码或解码失败。"""


@dataclass(frozen=True)
class Point:
    """一个整数像素坐标。"""

    x: int
    y: int

    def as_tuple(self) -> tuple[int, int]:
        """返回 (x, y) 元组。"""
        return (self.x, self.y)


@dataclass(frozen=True)
class Rect:
    """一个轴对齐矩形。"""

    x: int
    y: int
    w: int
    h: int

    def corners(self) -> list[Point]:
        """返回四个角点，顺序为左上、右上、右下、左下。"""
        return [
            Point(self.x, self.y),
            Point(self.x + self.w, self.y),
            Point(self.x + self.w, self.y + self.h),
            Point(self.x, self.y + self.h),
        ]
