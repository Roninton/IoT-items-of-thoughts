"""事实链里的依赖图必须与磁盘上的真实依赖一致（设计原文的风险对策）。

设计原文在风险表里写：「依赖图与真实代码不符 → 每轮用 AST 重新抽取真实依赖图，
与 C 中比对」。没有这条比对，"事实链可信"就只是一句声称：
C 可以带着一条早已不存在的依赖边继续往下走，而下游据此做错决策。

precision 与 recall 两端都要看 —— 只看 precision 会掩盖"漏记"，
只看 recall 会掩盖"编造"。

注意测试用的是**真轮次**（脚本 oracle），不是注入 O_i：
注入的 O_i 会被验证器回退、文件被撤掉，于是磁盘上什么都没有（第一版就这么错过）。
"""

from __future__ import annotations

from pathlib import Path

from pciso.models import RoundRequest, SessionCreateRequest
from pciso.session import Session

PREFIX = Path(__file__).resolve().parents[1] / "prefixes" / "drawing_video"


def make_session(tmp_path: Path, workspace: Path) -> Session:
    state_dir = tmp_path / "state"
    cfg = {
        "project": "drawing_video_toolchain",
        "arm": "DEP",
        "prefix_path": str(PREFIX),
        "state_dir": str(state_dir),
        "max_rounds": 8,
        "workspace_path": str(workspace),
        "branch_per_arm": False,
        "fake_oracle": "drawing_video",
        "compression": {"mode": "fact_chain", "model": "heuristic"},
        "work_model": {"endpoint": "fake"},
    }
    return Session.create(
        SessionCreateRequest(
            project="drawing_video_toolchain",
            prefix_path=str(PREFIX),
            config=cfg,
            arm="DEP",
        ),
        base_dir=tmp_path,
        state_dir=str(state_dir),
    )


def report(session: Session) -> dict:
    return session.metrics()["compression_quality"]["dep_graph_consistency"]


def test_consistent_graph_scores_perfectly(tmp_path: Path) -> None:
    ws = tmp_path / "target"
    ws.mkdir()
    session = make_session(tmp_path, ws)
    # 跑到有跨模块依赖为止（阶段 2 的素材层会 import core）
    for _ in range(3):
        session.round(RoundRequest())

    got = report(session)
    assert got["claimed"] >= 1, got
    assert got["actual"] >= 1, got
    assert got["precision"] == 1.0, got
    assert got["recall"] == 1.0, got


def test_external_imports_are_not_counted_as_module_deps(tmp_path: Path) -> None:
    """`import json` / `import zlib` 不是模块依赖 —— 不过滤会永远报假警报。"""
    ws = tmp_path / "target"
    ws.mkdir()
    session = make_session(tmp_path, ws)
    session.round(RoundRequest())

    got = report(session)
    assert got["recall"] in (None, 1.0), got
    assert not any("json" in x or "zlib" in x for x in got["missing_in_c"]), got


def test_code_diverging_from_c_is_caught(tmp_path: Path) -> None:
    """**这正是设计担心的那件事**：代码改了，C 里那条边还在。

    直接改磁盘（模拟"模型改了代码但事实链没跟上"），C 不会自己更新 ——
    比对必须把它报成 extra_in_c，而不是默默继续信它。
    """
    ws = tmp_path / "target"
    ws.mkdir()
    session = make_session(tmp_path, ws)
    for _ in range(2):
        session.round(RoundRequest())
    before = report(session)
    assert before["claimed"] >= 1, before
    assert before["precision"] == 1.0, before

    # 把被 C 声称的某条依赖边对应的源码改写成"不再依赖"的版本。
    # 用整文件改写而不是删 import 行：相对导入写成 `from .core import X` 时，
    # 按模块全名做字符串匹配会漏掉（第一版就这么漏过）。
    src, dst = sorted((d.src, d.dst) for d in session.state().deps)[0]
    rel = src.replace(".", "/") + ".py"
    (ws / rel).write_text(
        f'"""{src}（后来改成不依赖 {dst} 了）。"""\n\nVALUE = 1\n', encoding="utf-8"
    )

    after = report(session)
    assert after["precision"] < 1.0, after
    assert f"{src}->{dst}" in after["extra_in_c"], after


def test_tests_and_post_are_not_part_of_the_graph(tmp_path: Path) -> None:
    """测试目录与加固区不进依赖图 —— 否则它们会变成一堆假阳性边。"""
    ws = tmp_path / "target"
    (ws / "tests").mkdir(parents=True)
    (ws / "post").mkdir(parents=True)
    (ws / "tests" / "test_x.py").write_text("import drawvid.core\n", encoding="utf-8")
    (ws / "post" / "patch.py").write_text("import drawvid.core\n", encoding="utf-8")
    session = make_session(tmp_path, ws)

    got = report(session)
    assert got["actual"] == 0, got
