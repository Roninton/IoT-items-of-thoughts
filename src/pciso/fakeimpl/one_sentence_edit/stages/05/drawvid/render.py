"""渲染层：时间即画面。

本模块可依赖 core 与 assets；**不得**依赖 planner —— 渲染只管"怎么画"。

`render_frame` 是**纯函数**：同样的入参必定产出逐像素完全相同的帧。
不读时钟、不用随机、不读环境变量、不依赖当前目录、不留跨调用状态、
在渲染里不做任何 I/O。确定性不是风格偏好，它是"任意一帧可单独重算"的前提。
"""

from __future__ import annotations

from .assets import RGBA_CHANNELS, Image, new_image, resize
from .core import CaptionCue, Scene, Size, Timeline

BACKGROUND_MIN = 16
BACKGROUND_MAX = 200
BACKGROUND_SPAN = BACKGROUND_MAX - BACKGROUND_MIN
KENBURNS_MAX_SCALE = 1.06
PROGRESS_BAR_HEIGHT = 4
CAPTION_BAND_TOP_RATIO = 0.82
CAPTION_BAND_BOTTOM_RATIO = 0.90
CAPTION_BAND_X_NUM = 9
CAPTION_BAND_X_DEN = 10
WHITE = (255, 255, 255, 255)


def _clamp_ms(t_ms: int, duration_ms: int) -> int:
    """把时间钳到 `[0, duration_ms - 1]`。"""
    if duration_ms <= 0:
        return 0
    return min(max(int(t_ms), 0), duration_ms - 1)


def _find_scene(scenes: list[Scene], t_ms: int) -> Scene | None:
    """返回覆盖 `t_ms` 的镜头；没有镜头时返回 None。"""
    for scene in scenes:
        if scene.start_ms <= t_ms < scene.start_ms + scene.duration_ms:
            return scene
    return scenes[-1] if scenes else None


def _scene_progress(scene: Scene, t_ms: int) -> float:
    """返回镜头内的归一化进度 p ∈ [0, 1]；镜头时长为 0 时返回 0.0。"""
    if scene.duration_ms <= 0:
        return 0.0
    return (t_ms - scene.start_ms) / scene.duration_ms


def _kenburns_scale(effect: str, progress: float) -> float:
    """按效果返回缩放系数：in 从 1.0 放大到 1.06，out 从 1.06 缩回 1.0。"""
    if effect == "kenburns_in":
        return 1.0 + (KENBURNS_MAX_SCALE - 1.0) * progress
    if effect == "kenburns_out":
        return KENBURNS_MAX_SCALE - (KENBURNS_MAX_SCALE - 1.0) * progress
    return 1.0


def _background_pixel(scene_index: int, x: int, y: int, size: Size) -> tuple[int, int, int, int]:
    """返回确定性背景色：以镜头下标为唯一变量，三通道都落在 [16, 200]。"""
    half_w = max(size.width - 1, 1)
    half_h = max(size.height - 1, 1)
    ramp = (x * 160 // half_w) + (y * 96 // half_h)
    red = BACKGROUND_MIN + (ramp + scene_index * 37) % BACKGROUND_SPAN
    green = BACKGROUND_MIN + (ramp // 3 + scene_index * 53) % BACKGROUND_SPAN
    blue = BACKGROUND_MIN + ((half_w - x) * 120 // half_w + scene_index * 29) % BACKGROUND_SPAN
    return (red, green, blue, 255)


def _make_background(scene_index: int, size: Size) -> Image:
    """生成该镜头的整幅确定性渐变背景。"""
    image = Image(size.width, size.height)
    data = image._data
    channels = RGBA_CHANNELS
    raw_cursor = 0
    for y in range(size.height):
        for x in range(size.width):
            red, green, blue, alpha = _background_pixel(scene_index, x, y, size)
            data[raw_cursor] = red
            data[raw_cursor + 1] = green
            data[raw_cursor + 2] = blue
            data[raw_cursor + 3] = alpha
            raw_cursor += channels
    return image


def _draw_image(image: Image, source: Image, scale: float) -> None:
    """按中心锚点把素材位移缩放着铺满整幅画面。

    可见窗口是素材中心的一块 `(source / scale)` 区域，再被放大到整幅画面：
    `scale` 越大窗口越小、放大倍率越高，因此 1.0 → 1.06 是**真的在推近**。
    素材比画面小时窗口也不会退化成整幅素材 —— 否则缩放就成了空操作。
    """
    if source.width <= 0 or source.height <= 0:
        return
    if scale < 1.0:
        source = resize(
            source,
            max(1, int(source.width * scale)),
            max(1, int(source.height * scale)),
        )
        scale = 1.0
    view_w = max(1, min(source.width, int(source.width / scale)))
    view_h = max(1, min(source.height, int(source.height / scale)))
    left = (source.width - view_w) // 2
    top = (source.height - view_h) // 2

    # 采样坐标只由宽高决定，先按行/列各算一遍，避免逐像素重复做整数除法。
    max_x = source.width - 1
    max_y = source.height - 1
    src_xs = [min(left + (x * view_w) // image.width, max_x) for x in range(image.width)]
    src_ys = [min(top + (y * view_h) // image.height, max_y) for y in range(image.height)]
    source_data = source._data
    source_width = source.width
    data = image._data
    channels = RGBA_CHANNELS
    cursor = 0
    for src_y in src_ys:
        row_base = src_y * source_width * channels
        for src_x in src_xs:
            base = row_base + src_x * channels
            data[cursor] = source_data[base]
            data[cursor + 1] = source_data[base + 1]
            data[cursor + 2] = source_data[base + 2]
            data[cursor + 3] = source_data[base + 3]
            cursor += channels


def _draw_progress_bar(image: Image, t_ms: int, duration_ms: int) -> None:
    """底部 [H-4, H) 全宽画进度条：已完成宽度 = W * t // duration（严格线性）。

    刻意不做缓动。早先这里用过一条加速曲线，只是为了让一条**写错了的**测试断言变绿 ——
    断言当时要求"t=75% 时最右侧像素已白"，那与 P 的线性规格互相矛盾。
    矛盾要改断言，不要改规格：进度条是观众用来判断"还剩多久"的，加速会让它撒谎。
    """
    if duration_ms <= 0:
        return
    width = image.width
    clamped = _clamp_ms(t_ms, duration_ms)
    filled = min(width, width * clamped // duration_ms)
    if filled <= 0:
        return
    data = image._data
    channels = RGBA_CHANNELS
    row = bytes(WHITE) * filled
    for y in range(max(0, image.height - PROGRESS_BAR_HEIGHT), image.height):
        start = y * width * channels
        data[start : start + len(row)] = row


def _caption_covers(captions: list[CaptionCue], t_ms: int) -> bool:
    """返回是否有字幕 cue 覆盖 `t_ms`。"""
    return any(cue.start_ms <= t_ms < cue.end_ms for cue in captions)


def _draw_caption_band(image: Image) -> None:
    """把字幕提示带画成纯色矩形（画面内只画带，不画字）。"""
    left = image.width // 10
    right = image.width * CAPTION_BAND_X_NUM // CAPTION_BAND_X_DEN
    top = int(image.height * CAPTION_BAND_TOP_RATIO)
    bottom = int(image.height * CAPTION_BAND_BOTTOM_RATIO)
    span = right - left
    if span <= 0:
        return
    data = image._data
    channels = RGBA_CHANNELS
    row = bytes(WHITE) * span
    for y in range(top, bottom):
        start = (y * image.width + left) * channels
        data[start : start + len(row)] = row


def render_frame(timeline: Timeline, t_ms: int, images: dict[str, Image]) -> Image:
    """在 `t_ms` 处渲染一帧：确定性背景 + Ken Burns + 进度条 + 字幕提示带。

    必须是**纯函数**：同样入参两次调用逐像素相同。

    公开签名里刻意不放缓存参数。契约比对是逐字的，给公开接口加"可选"参数同样算签名漂移；
    需要跨帧复用的东西（背景、解好的素材）走下面的私有实现。
    """
    return _render_frame(timeline, t_ms, images, None)


def _render_frame(
    timeline: Timeline,
    t_ms: int,
    images: dict[str, Image],
    backgrounds: dict[int, Image] | None,
) -> Image:
    """`render_frame` 的私有实现；`backgrounds` 只做单次 `render_frames` 内的背景复用。"""
    duration_ms = timeline.duration_ms()
    clamped = _clamp_ms(t_ms, duration_ms)
    if not timeline.scenes:
        return new_image(
            timeline.size.width,
            timeline.size.height,
            _background_pixel(0, 0, 0, timeline.size),
        )

    scene = _find_scene(timeline.scenes, clamped)
    if scene is None:  # pragma: no cover - scenes 非空时不会发生
        return new_image(
            timeline.size.width,
            timeline.size.height,
            _background_pixel(0, 0, 0, timeline.size),
        )
    scene_index = timeline.scenes.index(scene)
    if backgrounds is not None and scene_index in backgrounds:
        image = _copy_image(backgrounds[scene_index])
    else:
        image = _make_background(scene_index, timeline.size)
        if backgrounds is not None:
            backgrounds[scene_index] = _copy_image(image)

    source = images.get(scene.asset_path) if scene.asset_path else None
    if source is not None:
        _draw_image(image, source, _kenburns_scale(scene.effect, _scene_progress(scene, clamped)))

    # 字幕带先画、进度条后画：小画布上两者会重叠，进度条压在上层，
    # 这样"进度条只由时间决定"这条性质在任何尺寸下都成立。
    if _caption_covers(timeline.captions, clamped):
        _draw_caption_band(image)
    _draw_progress_bar(image, clamped, duration_ms)
    return image


def _copy_image(image: Image) -> Image:
    """复制一张图（背景缓存要发给每一帧独立涂改）。"""
    return Image(image.width, image.height, bytearray(image._data))


def render_frames(timeline: Timeline, images: dict[str, Image]) -> list[Image]:
    """返回 `frame_count()` 张帧；第 i 张取 `t = i * 1000 // fps`。"""
    backgrounds: dict[int, Image] = {}
    return [
        _render_frame(timeline, index * 1000 // timeline.fps, images, backgrounds)
        for index in range(timeline.frame_count())
    ]
