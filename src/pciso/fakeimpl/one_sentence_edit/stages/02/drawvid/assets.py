"""素材层：纯标准库 PNG 解码与 RGBA 图像缓冲。

本模块只依赖 core。

解码范围是**刻意收窄**的：只支持 8 位色深的真彩 RGBA（color type 6），
filter 支持 0–4。调色板 / 灰度 / 16 位 PNG 一律抛 `DrawvidError` ——
纯标准库做完整 PNG 解码不现实，这是明确的工程取舍，不是缺陷。
"""

from __future__ import annotations

import struct
import zlib

from .core import CodecError, DrawvidError

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
RGBA_CHANNELS = 4
BYTE_RANGE = 256
EXPECTED_IHDR_LEN = 13
FILTER_NONE = 0
FILTER_SUB = 1
FILTER_UP = 2
FILTER_AVERAGE = 3
FILTER_PAETH = 4


class Image:
    """一块 8 位 RGBA 像素缓冲，逐通道 0–255。"""

    def __init__(self, width: int, height: int, data: bytearray | None = None) -> None:
        """按宽高建立缓冲；`data` 为 None 时全部填成透明黑。"""
        if width <= 0 or height <= 0:
            raise DrawvidError(f"非法图像尺寸 {width}x{height}，宽高必须为正整数")
        self._width = int(width)
        self._height = int(height)
        size = self._width * self._height * RGBA_CHANNELS
        if data is None:
            self._data = bytearray(size)
        else:
            if len(data) != size:
                raise DrawvidError(f"像素数据长度 {len(data)} 与 {width}x{height} 不符")
            self._data = bytearray(data)

    @property
    def width(self) -> int:
        """返回图像宽度（像素）。"""
        return self._width

    @property
    def height(self) -> int:
        """返回图像高度（像素）。"""
        return self._height

    def get_pixel(self, x: int, y: int) -> tuple[int, int, int, int]:
        """返回 (x, y) 处的 RGBA 四元组；越界坐标会被钳到边缘。"""
        cx = min(max(int(x), 0), self._width - 1)
        cy = min(max(int(y), 0), self._height - 1)
        offset = (cy * self._width + cx) * RGBA_CHANNELS
        return (
            self._data[offset],
            self._data[offset + 1],
            self._data[offset + 2],
            self._data[offset + 3],
        )

    def put_pixel(self, x: int, y: int, color: tuple[int, int, int, int]) -> None:
        """把 (x, y) 处写成 `color`；越界坐标会被钳到边缘。"""
        cx = min(max(int(x), 0), self._width - 1)
        cy = min(max(int(y), 0), self._height - 1)
        offset = (cy * self._width + cx) * RGBA_CHANNELS
        for index, channel in enumerate(color):
            self._data[offset + index] = int(channel) % BYTE_RANGE

    def _row_bytes(self, y: int) -> bytes:
        """返回第 y 行（不含 filter 字节）的原始 RGBA 字节。"""
        start = y * self._width * RGBA_CHANNELS
        return bytes(self._data[start : start + self._width * RGBA_CHANNELS])

    def _set_row_bytes(self, y: int, row: bytes) -> None:
        """把第 y 行整体写成 `row`（必须是恰好一行长度的 RGBA 字节）。"""
        start = y * self._width * RGBA_CHANNELS
        self._data[start : start + len(row)] = row


def new_image(width: int, height: int, color: tuple[int, int, int, int]) -> Image:
    """新建一张单色图像。"""
    image = Image(width, height)
    for y in range(image.height):
        for x in range(image.width):
            image.put_pixel(x, y, color)
    return image


def _iter_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """按 PNG 结构切块；签名 / 长度 / CRC 任一不对就抛 CodecError。"""
    if not data.startswith(PNG_SIGNATURE):
        raise CodecError("不是合法 PNG：签名不匹配")
    chunks: list[tuple[bytes, bytes]] = []
    offset = len(PNG_SIGNATURE)
    total = len(data)
    while offset < total:
        if offset + 8 > total:
            raise CodecError("PNG 块头被截断")
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        ctype = data[offset + 4 : offset + 8]
        end = offset + 8 + length
        if end + 4 > total:
            raise CodecError(f"{ctype!r} 块体被截断")
        payload = data[offset + 8 : end]
        (crc,) = struct.unpack(">I", data[end : end + 4])
        if crc != zlib.crc32(ctype + payload) & 0xFFFFFFFF:
            raise CodecError(f"{ctype!r} 块 CRC 校验失败")
        chunks.append((ctype, payload))
        offset = end + 4
    if not chunks or chunks[-1][0] != b"IEND":
        raise CodecError("PNG 缺少 IEND 结束块")
    return chunks


def _parse_ihdr(payload: bytes) -> tuple[int, int]:
    """解析 IHDR，返回 (width, height)；只接受 8 位 RGBA。"""
    if len(payload) != EXPECTED_IHDR_LEN:
        raise CodecError("IHDR 长度不是 13 字节")
    width, height, depth, color_type, compression, filter_method, interlace = struct.unpack(
        ">IIBBBBB", payload
    )
    if depth != 8:
        raise DrawvidError(f"只支持 8 位色深，收到 {depth} 位")
    if color_type != 6:
        raise DrawvidError(f"只支持真彩 RGBA（color type 6），收到 {color_type}")
    if compression != 0 or filter_method != 0:
        raise DrawvidError("只支持 compression=0 / filter=0 的 PNG")
    if interlace != 0:
        raise DrawvidError("不支持隔行扫描（Adam7）PNG")
    if width <= 0 or height <= 0:
        raise DrawvidError(f"非法图像尺寸 {width}x{height}")
    return int(width), int(height)


def _paeth(a: int, b: int, c: int) -> int:
    """PNG 的 Paeth 预测器。"""
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(raw: bytes, width: int, height: int) -> bytearray:
    """把逐行 filter 还原成紧凑 RGBA 像素数据。"""
    stride = width * RGBA_CHANNELS
    out = bytearray(stride * height)
    offset = 0
    for y in range(height):
        if offset >= len(raw):
            raise CodecError(f"像素数据在第 {y} 行提前结束")
        filter_type = raw[offset]
        offset += 1
        line = raw[offset : offset + stride]
        if len(line) != stride:
            raise CodecError(f"像素数据在第 {y} 行被截断")
        offset += stride
        target = y * stride
        prior = target - stride
        if filter_type == FILTER_NONE:
            out[target : target + stride] = line
        elif filter_type == FILTER_SUB:
            _unfilter_sub(out, line, target, stride)
        elif filter_type == FILTER_UP:
            _unfilter_up(out, line, target, prior, stride, y)
        elif filter_type == FILTER_AVERAGE:
            _unfilter_average(out, line, target, prior, stride, y)
        elif filter_type == FILTER_PAETH:
            _unfilter_paeth(out, line, target, prior, stride, y)
        else:
            raise CodecError(f"不支持的 filter 类型 {filter_type}")
    return out


def _unfilter_sub(out: bytearray, line: bytes, target: int, stride: int) -> None:
    """还原 filter 1（Sub）。"""
    for i in range(stride):
        left = out[target + i - RGBA_CHANNELS] if i >= RGBA_CHANNELS else 0
        out[target + i] = (line[i] + left) & 0xFF


def _unfilter_up(out: bytearray, line: bytes, target: int, prior: int, stride: int, y: int) -> None:
    """还原 filter 2（Up）。"""
    for i in range(stride):
        above = out[prior + i] if y > 0 else 0
        out[target + i] = (line[i] + above) & 0xFF


def _unfilter_average(
    out: bytearray, line: bytes, target: int, prior: int, stride: int, y: int
) -> None:
    """还原 filter 3（Average）。"""
    for i in range(stride):
        left = out[target + i - RGBA_CHANNELS] if i >= RGBA_CHANNELS else 0
        above = out[prior + i] if y > 0 else 0
        out[target + i] = (line[i] + (left + above) // 2) & 0xFF


def _unfilter_paeth(
    out: bytearray, line: bytes, target: int, prior: int, stride: int, y: int
) -> None:
    """还原 filter 4（Paeth）。"""
    for i in range(stride):
        left = out[target + i - RGBA_CHANNELS] if i >= RGBA_CHANNELS else 0
        above = out[prior + i] if y > 0 else 0
        upper_left = out[prior + i - RGBA_CHANNELS] if (y > 0 and i >= RGBA_CHANNELS) else 0
        out[target + i] = (line[i] + _paeth(left, above, upper_left)) & 0xFF


def load_png(path: str) -> Image:
    """读入 8 位 RGBA PNG 并返回 Image；其它 PNG 变体抛 DrawvidError。"""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise DrawvidError(f"无法读取 PNG：{path}（{exc}）") from exc

    chunks = _iter_chunks(data)
    width = 0
    height = 0
    idat = bytearray()
    seen_ihdr = False
    for ctype, payload in chunks:
        if ctype == b"IHDR":
            if seen_ihdr:
                raise CodecError("PNG 出现重复的 IHDR")
            seen_ihdr = True
            width, height = _parse_ihdr(payload)
        elif ctype == b"IDAT":
            if not seen_ihdr:
                raise CodecError("IDAT 出现在 IHDR 之前")
            idat.extend(payload)
        elif ctype in (b"PLTE", b"tRNS"):
            raise DrawvidError("只支持真彩 RGBA PNG，不支持调色板 / tRNS")
    if not seen_ihdr:
        raise CodecError("PNG 缺少 IHDR")
    if not idat:
        raise CodecError("PNG 没有 IDAT 像素数据")

    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise CodecError(f"IDAT 解压失败：{exc}") from exc

    return Image(width, height, _unfilter(raw, width, height))
