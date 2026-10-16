"""音频层：按时间轴合成 BGM（WAV）。

本模块只依赖 core。

只用 `wave` + `math` 合成：不采样、不调模型、不 shell out —— 这满足"无配音"的前提，
只有器乐。合成全程**确定性**：同一 Timeline 两次调用逐字节相同，
不读时钟、不用随机、不留跨调用状态。
"""

from __future__ import annotations

import math
import struct
import wave

from .core import Timeline

SAMPLE_RATE = 22050
CHANNELS = 1
BYTES_PER_SAMPLE = 2
PEAK = 32767
MASTER_GAIN = 0.42
HARMONIC_COUNT = 4
HARMONIC_FALLOFF = 1.6
CHORD_ROOT_HZ = 110.0
CHORD_MAJOR_SEMITONES = (0, 4, 7)
CHORD_MINOR_SEMITONES = (0, 3, 7)
CHORD_TONE_COUNT = 3
RISER_HZ_LOW = 220.0
RISER_HZ_HIGH = 880.0
RISER_MS = 500
HIT_DECAY_MS = 120.0
FADE_MS = 8.0
TWO_PI = 2.0 * math.pi


def _chord_hz(scene_index: int) -> float:
    """返回该镜头对应的和弦根音频率（在三个音级之间循环）。"""
    return CHORD_ROOT_HZ * (2.0 ** ((scene_index % 3) - 1))


def _chord_intervals(scene_index: int) -> tuple[int, ...]:
    """偶数镜头用大三和弦，奇数镜头用小三和弦。"""
    return CHORD_MAJOR_SEMITONES if scene_index % 2 == 0 else CHORD_MINOR_SEMITONES


def _semitone_ratio(semitones: int) -> float:
    """把半音数换算成频率比。"""
    return 2.0 ** (semitones / 12.0)


def _tone(seconds: float, frequency: float) -> float:
    """一个带奇次泛音的音色值，避免纯正弦的单调。"""
    value = 0.0
    for harmonic in range(1, HARMONIC_COUNT + 1):
        value += math.sin(TWO_PI * frequency * harmonic * seconds) / (harmonic**HARMONIC_FALLOFF)
    return value


def _pad_sample(seconds: float, scene_index: int) -> float:
    """和弦垫音：三个音级叠加，整体做缓慢的音量起伏。"""
    root = _chord_hz(scene_index)
    value = 0.0
    for semitones in _chord_intervals(scene_index):
        value += _tone(seconds, root * _semitone_ratio(semitones))
    swell = 0.7 + 0.3 * math.sin(TWO_PI * 0.25 * seconds)
    return value * swell / CHORD_TONE_COUNT


def _hit_sample(seconds: float, scene_index: int) -> float:
    """镜头起点的打击感：指数衰减的低频脉冲。"""
    if seconds < 0.0 or seconds > HIT_DECAY_MS / 1000.0:
        return 0.0
    decay = math.exp(-seconds * 1000.0 / (HIT_DECAY_MS / 3.0))
    return decay * _tone(seconds, _chord_hz(scene_index) * 2.0)


def _riser_sample(elapsed_ms: float, absolute_seconds: float) -> float:
    """镜头切换前的上行音：频率在 500ms 内由低到高扫过，音量两端收拢。"""
    if elapsed_ms < 0.0 or elapsed_ms > RISER_MS:
        return 0.0
    progress = elapsed_ms / RISER_MS
    frequency = RISER_HZ_LOW + (RISER_HZ_HIGH - RISER_HZ_LOW) * progress
    envelope = progress * (1.0 - progress) * 4.0
    return envelope * _tone(absolute_seconds, frequency) * 0.6


def _envelope(sample_index: int, total: int, rate: int) -> float:
    """首尾各做一小段淡入淡出，避免起止爆音。"""
    fade = max(1, int(rate * FADE_MS / 1000.0))
    if sample_index < fade:
        return sample_index / fade
    if sample_index >= total - fade:
        return max(0.0, (total - sample_index) / fade)
    return 1.0


def _scene_index_at(offsets: list[int], t_ms: int) -> int:
    """二分查找覆盖 t_ms 的镜头下标（offsets 递增）。"""
    low = 0
    high = len(offsets) - 1
    while low < high:
        mid = (low + high + 1) // 2
        if offsets[mid] <= t_ms:
            low = mid
        else:
            high = mid - 1
    return low


def _pcm_bytes(timeline: Timeline, total: int) -> bytes:
    """逐采样合成 PCM，返回 16 位小端单声道字节流。"""
    offsets = sorted({scene.start_ms for scene in timeline.scenes}) or [0]
    if offsets[0] != 0:
        offsets = [0, *offsets]
    events = sorted(timeline.events, key=lambda event: (event.t_ms, event.kind))
    data = bytearray()
    for index in range(total):
        t_ms = index * 1000 // SAMPLE_RATE
        seconds = index / SAMPLE_RATE
        scene_index = _scene_index_at(offsets, t_ms)
        value = _pad_sample(seconds, scene_index)
        for event in events:
            delta_ms = t_ms - event.t_ms
            if event.kind == "hit":
                value += _hit_sample(delta_ms / 1000.0, scene_index)
            elif event.kind == "riser":
                value += _riser_sample(delta_ms, seconds)
        value *= MASTER_GAIN * _envelope(index, total, SAMPLE_RATE)
        value = max(-1.0, min(1.0, value))
        data.extend(struct.pack("<h", int(value * PEAK)))
    return bytes(data)


def synthesize_bgm(timeline: Timeline, out_path: str) -> str:
    """按时间轴合成单声道 / 16 位 / 22050Hz 的 WAV 器乐，返回路径。

    采样数严格等于 `duration_ms * 22050 // 1000`。
    """
    duration_ms = timeline.duration_ms()
    total = duration_ms * SAMPLE_RATE // 1000
    pcm = _pcm_bytes(timeline, total)
    try:
        with wave.open(out_path, "wb") as handle:
            handle.setnchannels(CHANNELS)
            handle.setsampwidth(BYTES_PER_SAMPLE)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(pcm)
    except OSError as exc:
        raise OSError(f"无法写入 WAV：{out_path}（{exc}）") from exc
    return out_path
