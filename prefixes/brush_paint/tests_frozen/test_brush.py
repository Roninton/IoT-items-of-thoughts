"""冻结验收测试（P 提供，只读；每轮由 server 从 P 恢复，改它无效）。

只走 CLI 契约：
    python -m brushkit --script <ops.json> --out <dir>

断言的是需求原文里**可客观判定**的四件事：
  1. 输出恒为 `<out>/current-view.jpg` 这一个文件，画多少次都不长数量（要求 2）；
  2. 连续撤回精确回到上一状态 —— 不多（不是清空画布）、不少（确实撤掉了那笔）（要求 3）；
  3. 撤到空画布再撤是 no-op（要求 3）；
  4. 同一脚本两次运行逐字节相同（要求 4 禁随机 / 禁时钟）。

注意第 5 条隐含断言：什么都不画 vs 画一笔，字节必须不同 ——
否则上面几条都能被"程序什么都不做"满足。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

# 顶层 import 契约模块（P 的硬要求，见 tests/test_prefix.py）：
# 模块不存在时，pytest 在**收集期**就报错 —— 而不是把"没实现"变成静默跳过。
# 断言本身只走 CLI 契约（对实现细节不设限），这些 import 只负责"模块名与符号名必须存在"。
from brushkit.cli import run
from brushkit.core import Canvas, Stroke, blank_canvas
from brushkit.export import write_current_view
from brushkit.raster import stroke_pixels
from brushkit.undo import UndoStack

ROOT = Path(__file__).resolve().parents[2]
VIEW = "current-view.jpg"
W, H = 64, 48

RED = {"kind": "stroke", "x0": 12, "y0": 10, "x1": 40, "y1": 30,
       "radius": 6, "color": [255, 0, 0], "alpha": 1.0}
BLUE = {"kind": "stroke", "x0": 48, "y0": 34, "x1": 20, "y1": 14,
        "radius": 4, "color": [0, 0, 255], "alpha": 0.5}
UNDO = {"kind": "undo"}


def _out_dir(tmp_path: Path, name: str) -> Path:
    out = tmp_path / f"out-{name}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _run(tmp_path: Path, ops: list, out: Path) -> subprocess.CompletedProcess:
    script = tmp_path / f"ops-{abs(hash(json.dumps(ops))) % 10**8}.json"
    script.write_text(
        json.dumps({"canvas": {"width": W, "height": H}, "ops": ops}, ensure_ascii=False),
        encoding="utf-8",
    )
    return subprocess.run(
        [sys.executable, "-m", "brushkit", "--script", str(script), "--out", str(out)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def _draw(tmp_path: Path, ops: list, name: str, out: Path | None = None) -> bytes:
    """跑一次 CLI，返回 current-view.jpg 的字节。"""
    out = out or _out_dir(tmp_path, name)
    proc = _run(tmp_path, ops, out)
    assert proc.returncode == 0, (
        f"CLI 退出码 {proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    view = out / VIEW
    assert view.is_file(), (
        f"没有生成 {VIEW}；目录里是 {sorted(p.name for p in out.rglob('*'))}"
    )
    return view.read_bytes()


def test_output_is_one_fixed_file(tmp_path: Path) -> None:
    """画多少次，输出目录里都只有一个 current-view.jpg，且内容确实被覆盖。"""
    out = _out_dir(tmp_path, "fixed")
    first = _draw(tmp_path, [RED, BLUE, RED], "fixed-a", out=out)
    after_first = sorted(p.name for p in out.rglob("*") if p.is_file())
    assert after_first == [VIEW], f"首次输出后目录里是 {after_first}，应当只有 {VIEW}"

    second = _draw(tmp_path, [BLUE], "fixed-b", out=out)
    after_second = sorted(p.name for p in out.rglob("*") if p.is_file())
    assert after_second == [VIEW], f"第二次输出后目录里是 {after_second} —— 有兜底命名/堆积"
    assert first != second, "第二次输出跟第一次一模一样 —— 覆盖写没生效或者根本没重画"


def test_consecutive_undo_returns_to_previous_state(tmp_path: Path) -> None:
    """连续撤回：撤两笔回到空画布，撤一笔回到只剩第一笔。"""
    blank = _draw(tmp_path, [], "blank")
    one = _draw(tmp_path, [RED], "one")
    two = _draw(tmp_path, [RED, BLUE], "two")
    undo_one = _draw(tmp_path, [RED, BLUE, UNDO], "undo-one")
    undo_two = _draw(tmp_path, [RED, BLUE, UNDO, UNDO], "undo-two")

    assert blank != one, "画了一笔跟空画布字节相同 —— 根本没画上去"
    assert one != two, "两笔跟一笔字节相同 —— 第二笔没生效"
    assert undo_one == one, "撤回一笔没有精确回到『只剩第一笔』的状态"
    assert undo_two == blank, "连续撤回两笔没有回到空画布"


def test_undo_on_empty_canvas_is_a_noop(tmp_path: Path) -> None:
    """空画布上继续撤：不报错、退出码 0、结果与空画布一致。"""
    blank = _draw(tmp_path, [], "blank2")
    over = _draw(tmp_path, [UNDO, UNDO, UNDO], "over")
    assert over == blank, "撤到空之后继续撤改变了画面 —— 应当是 no-op"


def test_two_runs_are_byte_identical(tmp_path: Path) -> None:
    """同一脚本跑两次逐字节相同（禁随机 / 禁时钟 / 禁不确定遍历序）。"""
    ops = [RED, BLUE, UNDO, RED]
    a = _draw(tmp_path, ops, "det-a")
    b = _draw(tmp_path, ops, "det-b")
    c = _draw(tmp_path, ops, "det-c")
    ha, hb, hc = (hashlib.sha256(x).hexdigest() for x in (a, b, c))
    assert ha == hb == hc, f"同一脚本三次运行结果不同：{ha[:12]} / {hb[:12]} / {hc[:12]}"
