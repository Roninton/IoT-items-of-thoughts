"""一次性探针：压缩器 prompt 的「可缓存前缀」到底有多长。

背景：真机实测压缩器的缓存命中恒为 0（log 里 compressor_cached_tokens 全 0），
而工作模型能稳定命中（P=2225 tokens 那段）。想知道压缩器前缀里第一处「每轮都变」的
文字出现在哪个位置 —— 那就决定了它最多能缓存多少。

跑法（cwd=仓库根、PYTHONPATH=src）：
    python out/_probe_head.py
"""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")

from pciso.render import build_compressor_prompt, est_tokens  # noqa: E402

p1 = build_compressor_prompt(o_i="我这轮什么都没干。", round_index=0, c_max_tokens=2000, out_max_tokens=1200)
p2 = build_compressor_prompt(
    o_i="我改完了 brushkit/raster.py 的 stroke_pixels，加了胶囊体距离场。" * 20,
    round_index=5,
    c_max_tokens=2000,
    out_max_tokens=1200,
    prior_nodes=["N1 -> brushkit/core.py :: Canvas", "N2 -> brushkit/raster.py :: stroke_pixels"],
    prior_failures=["硬约束 C12 新违反: layer_violation"],
    units=["# 单位 1：叙述", "改完了 raster"],
)

n = 0
while n < min(len(p1), len(p2)) and p1[n] == p2[n]:
    n += 1

head = p1[:n]
print("第一处不同出现在第", n, "个字符")
print("可缓存前缀 =", est_tokens(head), "tokens（p1 全长", est_tokens(p1), "）")
print("--- 前缀结尾 ---")
print(repr(head[-90:]))
print("--- 断点后 ---")
print(repr(p2[n : n + 90]))
