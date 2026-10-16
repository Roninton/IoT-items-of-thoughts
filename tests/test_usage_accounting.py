"""开销表的形状要能自己证明自己。

为什么单独测这个：`_accumulate_usage` 以前对不认识的键**静默丢弃**，
于是 `compressor_cached_tokens` 被吞掉两轮，报告里显示"压缩器 0 命中" ——
一个纯粹的表结构 bug，却会被读成"缓存没生效"这种机制结论。
这里的用例就是钉住这条：加字段漏改一处必须吵出来。
"""

from __future__ import annotations

import pytest

from pciso.models import Usage
from pciso.session import _accumulate_usage


def test_usage_tracks_compressor_cache() -> None:
    assert hasattr(Usage(), "compressor_cached_tokens")


def test_accumulate_usage_sums_compressor_cache() -> None:
    usage = Usage()
    _accumulate_usage(usage, {"compressor_prompt_tokens": 2000, "compressor_cached_tokens": 1472})
    _accumulate_usage(usage, {"compressor_prompt_tokens": 100, "compressor_cached_tokens": 64})
    assert usage.compressor_prompt_tokens == 2100
    assert usage.compressor_cached_tokens == 1536


def test_accumulate_usage_rejects_unknown_key() -> None:
    """静默丢弃 = "这个开销永远是 0"，比报错难查得多。"""
    with pytest.raises(ValueError, match="compressor_oops_tokens"):
        _accumulate_usage(Usage(), {"compressor_oops_tokens": 1})


def test_usage_merge_keeps_compressor_cache() -> None:
    a = Usage(compressor_cached_tokens=1000)
    b = Usage(compressor_cached_tokens=472)
    merged = a.merge(b)
    assert merged.compressor_cached_tokens == 1472
    assert merged.work_cached_tokens == 0
