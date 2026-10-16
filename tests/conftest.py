"""测试公共设施。

## 为什么有 `slow` 标记

整套测试约 4 分钟，其中两项占 47%：

  - `test_preview.py`：每个测试真渲染一遍纯 Python 成片（已把句子的时长从默认
    12000ms 改成 "1 秒" = 30 帧，省掉约 39s）
  - `test_mechanism_invariants.py`：每个种子每轮都起真 pytest+ruff 子进程
    （已把默认种子数从 24 降到 8，可用 `PCISO_INVARIANT_SEEDS=24` 跑满）

TDD 循环要的是**秒级反馈**，所以把端到端/带子进程的慢测试标上 `slow`：

    python -m pytest -q -m "not slow"     # 快车道，改机制时用
    python -m pytest -q                   # 全量，提交前用

慢测试不是不重要 —— 它们是端到端与真机级别的验证，只是不该出现在每次小改的循环里。
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "slow: 端到端或要起子进程（pytest/ruff/渲染），用 -m 'not slow' 跳过",
    )
