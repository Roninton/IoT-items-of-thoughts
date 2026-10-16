"""绘图基元与画布。"""

from __future__ import annotations

from .core import DrawvidError, Point, Rect


def line(x0: int, y0: int, x1: int, y1: int) -> list[Point]:
    """Bresenham 直线光栅化，返回含首尾端点的点列。"""
    points: list[Point] = []
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        points.append(Point(x0, y0))
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy
    return points


def rect(r: Rect) -> list[Point]:
    """矩形轮廓光栅化：四条边首尾相接，角点不重复。"""
    out: list[Point] = []
    out.extend(line(r.x, r.y, r.x + r.w, r.y)[:-1])
    out.extend(line(r.x + r.w, r.y, r.x + r.w, r.y + r.h)[:-1])
    out.extend(line(r.x + r.w, r.y + r.h, r.x, r.y + r.h)[:-1])
    out.extend(line(r.x, r.y + r.h, r.x, r.y)[:-1])
    return out


def ellipse(cx: int, cy: int, rx: int, ry: int) -> list[Point]:
    """整数椭圆光栅化：按角度采样并去重保序。"""
    import math

    if rx <= 0 or ry <= 0:
        return [Point(cx, cy)]
    seen: set[tuple[int, int]] = set()
    out: list[Point] = []
    steps = max(8, int(2 * math.pi * max(rx, ry)))
    for i in range(steps):
        t = 2 * math.pi * i / steps
        x = cx + round(rx * math.cos(t))
        y = cy + round(ry * math.sin(t))
        if (x, y) not in seen:
            seen.add((x, y))
            out.append(Point(x, y))
    return out


class Canvas:
    """内存画布：累积折线路径，可导出为 SVG。"""

    def __init__(self, width: int, height: int) -> None:
        """创建 width x height 的画布。"""
        if width <= 0 or height <= 0:
            raise DrawvidError("画布尺寸必须为正")
        self.width = width
        self.height = height
        self._paths: list[tuple[list[Point], str, int]] = []

    def add(self, points: list[Point], color: str = "#000000", width: int = 1) -> int:
        """添加一条折线路径，返回其序号。"""
        if not points:
            raise DrawvidError("路径不能为空")
        self._paths.append((list(points), color, width))
        return len(self._paths) - 1

    def path_count(self) -> int:
        """返回画布上的路径条数。"""
        return len(self._paths)

    def to_svg(self) -> str:
        """导出为 SVG 字符串。"""
        parts = [
            (
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" '
                f'height="{self.height}" viewBox="0 0 {self.width} {self.height}">'
            )
        ]
        for points, color, width in self._paths:
            d = " ".join(
                ("M" if i == 0 else "L") + f"{p.x} {p.y}" for i, p in enumerate(points)
            )
            parts.append(
                f'<path d="{d}" stroke="{color}" stroke-width="{width}" fill="none"/>'
            )
        parts.append("</svg>")
        return "".join(parts)
