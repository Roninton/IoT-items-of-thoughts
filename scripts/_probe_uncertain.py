"""临时探针：把某个会话某轮 thinking 里 UNCERTAINTY_RE 的命中点连同上下文写到 out/ 下的
UTF-8 文件（GBK 控制台打不出中文，只能落盘再看）。用完可删。"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pciso.actnodes import UNCERTAINTY_RE  # noqa: E402

sid = sys.argv[1]
index = int(sys.argv[2]) if len(sys.argv) > 2 else 0
path = ROOT / "state" / "sessions" / sid / "thinking" / f"{index:03d}.txt"
text = path.read_text(encoding="utf-8")
lines: list[str] = [f"# {path}（{len(text)} 字）"]
for m in UNCERTAINTY_RE.finditer(text):
    lines.append(f"\n--- pos={m.start()} match={m.group(0)!r}")
    lines.append("前: " + text[max(0, m.start() - 100) : m.start()].replace("\n", "⏎"))
    lines.append("后: " + text[m.end() : m.end() + 100].replace("\n", "⏎"))
out = ROOT / "out" / "uncertain-probe.txt"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text("\n".join(lines), encoding="utf-8")
print(out)
