"""两条用户裁定（机制语义，不是口味）。

1. **文件块路径全被拒 = 失败尝试 -> 必须进失败链。**
   路径越界/空文件块是 server 客观判定的事实，不能只躺在 `validation.errors` 里 ——
   那份东西下一轮看不到，于是模型会反复走同一条死路。
   失败链存在的全部意义就是"试过 X，因 Y 否决"。

2. **正文为空 -> 重试，而不是判这一轮失败。**
   正文为空几乎总是"思考吃光了输出预算"（`finish_reason=length`）。
   实测 `max_tokens=8000` 时 31996 字符思考、正文 0 字；给到 32000 就正常 ——
   而平台自己的 DSH 配置对同一适配器用的也是 32768（注释：够用，还含 thinking）。
   所以正常情况不会发生，根源是窗口太小；真出现了就重试，且**重试的花费必须累加**。
"""

from __future__ import annotations

from pathlib import Path

from pciso.config import WorkModelConfig
from pciso.models import RoundRequest, SessionCreateRequest
from pciso.session import Session
from pciso.workmodels import ChatResult, OpenAICompatWorkModel

PREFIX = Path(__file__).resolve().parents[1] / "prefixes" / "drawing_video"


def make_session(tmp_path: Path, *, workspace: Path | None = None) -> Session:
    ws = workspace or (tmp_path / "target")
    ws.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    cfg = {
        "project": "drawing_video_toolchain",
        "arm": "RULING",
        "prefix_path": str(PREFIX),
        "state_dir": str(state_dir),
        "max_rounds": 4,
        "workspace_path": str(ws),
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
            arm="RULING",
        ),
        base_dir=tmp_path,
        state_dir=str(state_dir),
    )


# --------------------------------------------------------------------------- #
# 裁定 1：被拒的落盘尝试进失败链
# --------------------------------------------------------------------------- #


def test_rejected_paths_become_failure_chain_entries(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    o_i = (
        "本轮试着写两个文件。\n\n"
        "```file:drawvid/core.py\n"
        '"""核心。"""\n\n\nclass Point:\n    """点。"""\n'
        "```\n\n"
        "```file:../escape.py\n"
        "VALUE = 1\n"
        "```\n\n"
        "```file:notes.txt\n"
        "随便写点\n"
        "```\n"
    )
    session.round(RoundRequest(o_i=o_i))
    record = session.store.last_round()
    chain = record["c_i"]["failure_chain"]

    assert chain, f"被拒的落盘尝试没有进失败链：{record['merge_report']}"
    server_entries = [f for f in chain if f.get("source") == "server"]
    assert server_entries, f"失败链里没有 server 来源的条目：{chain}"
    joined = " ".join(f"{f['attempt']} {f['reason']}" for f in server_entries)
    assert "../escape.py" in joined, joined
    assert record["telemetry"]["rejected_paths"] >= 1

    # 失败链进了**下一轮的上下文**（这才是它存在的意义）
    session.round(RoundRequest(o_i="```file:drawvid/core.py\nVALUE = 2\n```\n"))
    next_prompt = session.store.last_round()["prompt"]
    assert "../escape.py" in next_prompt, "失败链没有进下一轮 prompt"


def test_empty_file_block_is_also_a_recorded_failure(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    o_i = '```file:drawvid/core.py\n```\n'
    session.round(RoundRequest(o_i=o_i))
    record = session.store.last_round()
    joined = " ".join(
        f"{f['attempt']} {f['reason']}" for f in record["c_i"]["failure_chain"]
    )
    assert "drawvid/core.py" in joined, joined


def test_failure_source_is_distinguishable(tmp_path: Path) -> None:
    """来源必须可区分：提取器读出来的 vs server 判定的。"""
    session = make_session(tmp_path)
    o_i = (
        "被否决的方案：试 Z，因职责耦合否决。\n\n"
        "```file:../bad.py\nVALUE = 1\n```\n"
    )
    session.round(RoundRequest(o_i=o_i))
    chain = session.store.last_round()["c_i"]["failure_chain"]
    sources = {f.get("source") for f in chain}
    assert "server" in sources, chain
    assert sources <= {"extractor", "server"}, sources


# --------------------------------------------------------------------------- #
# 裁定 2：正文为空要重试
# --------------------------------------------------------------------------- #


class StubClient:
    """按脚本依次返回；记录每次调用的 max_tokens。"""

    def __init__(self, replies: list[ChatResult]) -> None:
        self.replies = list(replies)
        self.calls: list[int] = []

    def chat(self, prompt: str, **kwargs: object) -> ChatResult:
        self.calls.append(int(kwargs.get("max_tokens") or 0))
        if self.replies:
            return self.replies.pop(0)
        return ChatResult(text="```file:drawvid/core.py\nVALUE = 1\n```\n")


def make_model(client: StubClient, **over: object) -> OpenAICompatWorkModel:
    cfg = WorkModelConfig(**{"endpoint": "openai", "model": "stub", **over})
    model = OpenAICompatWorkModel(cfg)
    model.client = client  # type: ignore[assignment]
    return model


THINKING_ONLY = ChatResult(
    text="我在反复权衡……", prompt_tokens=100, completion_tokens=8000,
    reasoning_chars=32000, thinking_fallback=True,
)
GOOD_ANSWER = ChatResult(
    text="```file:drawvid/core.py\nVALUE = 1\n```\n", prompt_tokens=100,
    completion_tokens=200, reasoning_chars=500,
)


def test_empty_answer_is_retried_with_a_bigger_budget() -> None:
    client = StubClient([THINKING_ONLY, GOOD_ANSWER])
    model = make_model(client, max_tokens=8000, retry_on_empty=1)
    out = model.run("p", stage=1, round_index=1)

    assert out.retries == 1, out
    assert out.text.strip(), "重试后仍拿空正文"
    assert len(client.calls) == 2
    assert client.calls[1] > client.calls[0], f"重试没有加大预算：{client.calls}"
    assert "重试" in out.note


def test_retry_cost_is_accumulated_not_swallowed() -> None:
    """重试是真实花费 —— 只记最后一次等于把钱吞掉。"""
    client = StubClient([THINKING_ONLY, GOOD_ANSWER])
    out = make_model(client, max_tokens=8000, retry_on_empty=1).run(
        "p", stage=1, round_index=1
    )
    assert out.usage["work_prompt_tokens"] == 200  # 两次各 100
    assert out.usage["work_completion_tokens"] == 8200  # 8000 + 200
    assert out.reasoning_chars == 32500  # 两次思考都要算


def test_no_retry_when_the_answer_is_fine() -> None:
    client = StubClient([GOOD_ANSWER])
    out = make_model(client, max_tokens=8000, retry_on_empty=1).run(
        "p", stage=1, round_index=1
    )
    assert out.retries == 0
    assert len(client.calls) == 1


def test_retry_is_bounded_and_gives_up(tmp_path: Path) -> None:
    """重试有上限；用完了就照实返回空正文（由下游按"无产出"处置）。"""
    client = StubClient([THINKING_ONLY, THINKING_ONLY, THINKING_ONLY])
    out = make_model(client, max_tokens=8000, retry_on_empty=1).run(
        "p", stage=1, round_index=1
    )
    assert len(client.calls) == 2, "重试次数超了上限"
    assert out.retries == 1


def test_retry_can_be_disabled() -> None:
    client = StubClient([THINKING_ONLY, GOOD_ANSWER])
    out = make_model(client, max_tokens=8000, retry_on_empty=0).run(
        "p", stage=1, round_index=1
    )
    assert len(client.calls) == 1
    assert out.retries == 0
    assert out.thinking_fallback is True


# --------------------------------------------------------------------------- #
# 上下文纯净度：不能有隐藏的 system 输入
# --------------------------------------------------------------------------- #


def test_no_system_message_is_inserted_by_default() -> None:
    """实验前提是"工作模型只看到 P + C + 指令 + 任务 + 格式"。

    多一条 system 就是多一个**隐藏输入**，实验就在测别的东西了。
    实测抓过 payload：1 条 user、无 system、无历史。这里把"没插 system"变成可核对的数。
    """
    seen: list[dict] = []

    class Recorder(StubClient):
        def chat(self, prompt: str, **kwargs: object) -> ChatResult:
            seen.append(dict(kwargs))
            return super().chat(prompt, **kwargs)

    client = Recorder([GOOD_ANSWER])
    out = make_model(client, max_tokens=8000).run("p", stage=1, round_index=1)

    assert seen[0].get("system") in (None, ""), seen[0].get("system")
    assert out.system_chars == 0


def test_a_system_message_would_be_visible_in_the_trajectory() -> None:
    """源码里保留了 `system=` 这个 seam（将来做对照实验用）。

    一旦真有人用它，轨迹里必须立刻看得出来 —— 否则就是"悄悄改了实验条件"。
    """
    client = StubClient([GOOD_ANSWER])
    cfg = WorkModelConfig(endpoint="openai", model="stub")
    model = OpenAICompatWorkModel(cfg, system="你是一个隐藏的助手")
    model.client = client  # type: ignore[assignment]
    out = model.run("p", stage=1, round_index=1)
    assert out.system_chars == len("你是一个隐藏的助手")


def test_round_telemetry_records_system_chars(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    session.round(RoundRequest(o_i="```file:drawvid/core.py\nVALUE = 1\n```\n"))
    telemetry = session.store.last_round()["telemetry"]
    assert telemetry.get("work_system_chars", 0) == 0
