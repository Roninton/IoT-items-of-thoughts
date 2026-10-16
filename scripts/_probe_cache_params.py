"""一次性探针：为什么压缩器的前缀缓存命中率是 0，而工作模型能命中。

两条调用走的是**同一个 endpoint、同一个 model、同一个 key**，差别只在请求参数：
  工作模型：max_tokens=32000, temperature=0.2, thinking=True,  无 response_format
  压缩器  ：max_tokens=1200,  temperature=0.0, thinking=False, response_format=json_object
所以怀疑就落在这几个参数上（或者落在"前缀太短"上 —— 已经排除：prefix 1529 tokens 仍 0 命中）。

做法：同一段固定材料各发两次，看第二次的 prompt_cache_hit_tokens。
"""

from __future__ import annotations

import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

from pciso.workmodels import ModelClient  # noqa: E402

BASE = os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1"
MODEL = os.environ.get("PCISO_PROBE_MODEL") or "deepseek-flash"
print("base_url =", BASE, "| model =", MODEL)

# ~1600 tokens 的固定材料：前缀里没有一处会变
BODY = "只输出 {\"ok\": true}，不要解释。\n\n以下是固定材料：\n" + ("这是一段用于测量前缀缓存的固定材料。" * 120)


def probe(tag: str, **kw: object) -> None:
    client = ModelClient(base_url=BASE, model=MODEL)
    res = client.chat(BODY, **kw)  # type: ignore[arg-type]
    print(
        f"{tag:<28} prompt={res.prompt_tokens:>6} cached={res.cached_tokens:>6} "
        f"completion={res.completion_tokens:>5} err={res.error}"
    )


probe("压缩器参数 第1次", max_tokens=1200, temperature=0.0, thinking=False, response_format="json_object")
probe("压缩器参数 第2次", max_tokens=1200, temperature=0.0, thinking=False, response_format="json_object")
probe("压缩器参数·去 json 第1次", max_tokens=1200, temperature=0.0, thinking=False)
probe("压缩器参数·去 json 第2次", max_tokens=1200, temperature=0.0, thinking=False)
probe("工作模型参数 第1次", max_tokens=2000, temperature=0.2, thinking=True)
probe("工作模型参数 第2次", max_tokens=2000, temperature=0.2, thinking=True)
