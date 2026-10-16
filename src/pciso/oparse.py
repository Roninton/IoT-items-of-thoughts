"""O_i 的解析与落盘。

O_i 的契约（写在这里就是写死）：

    [自由叙述文本]
    ```file:相对路径
    文件完整内容
    ```
    [自由叙述文本] ...

为什么 O_i 用这种形态而不是工具调用轨迹：
  - 轮边界干净，B 组历史 = 前几轮 O_i 的拼接，A/B/C 对照才成立；
  - 成本可预算（max_tokens 直接夹住一整轮）；
  - 可复现、可回放、可离线重算，不依赖任何 agent 框架。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .models import Checkpoint, FileWrite

FILE_BLOCK_RE = re.compile(
    r"^[ \t]*```[ \t]*file:[ \t]*(?P<path>[^\s`]+)[ \t]*\r?\n(?P<body>.*?)^[ \t]*```[ \t]*$",
    re.DOTALL | re.MULTILINE,
)

# 协议标记行：`@@UNIT ...` / `@@READ ...`。它们是给 server 的指令，不是散文。
MARKER_LINE_RE = re.compile(r"^@@(?:UNIT|READ)\b")


@dataclass
class ParsedOutput:
    narrative: str = ""
    files: list[FileWrite] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def paths(self) -> list[str]:
        return [f.path for f in self.files]


def is_safe_relpath(path: str) -> tuple[bool, str]:
    p = PurePosixPath(path)
    if p.is_absolute():
        return False, "绝对路径"
    if any(part == ".." for part in p.parts):
        return False, "含 .. 越界"
    if not p.parts:
        return False, "空路径"
    if p.parts[0] == ".git" or ".git" in p.parts:
        return False, "禁止写入 .git"
    if path.endswith("/"):
        return False, "路径以 / 结尾"
    return True, ""


def parse_o_i(o_i: str, *, require_files: bool = True) -> ParsedOutput:
    out = ParsedOutput()
    spans: list[tuple[int, int]] = []

    for m in FILE_BLOCK_RE.finditer(o_i or ""):
        spans.append((m.start(), m.end()))
        raw_path = m.group("path").strip().strip("`'\"")
        body = m.group("body")
        ok, why = is_safe_relpath(raw_path)
        if not ok:
            out.errors.append(f"非法路径 {raw_path!r}: {why}")
            continue
        if not body.endswith("\n"):
            body += "\n"
        if not body.strip():
            out.errors.append(f"空文件块 {raw_path!r}")
            continue
        out.files.append(FileWrite.make(raw_path, body))

    # narrative = 去掉所有文件块、**以及协议标记行**之后的自由叙述。
    #
    # 只剥 file 块是不够的：`@@UNIT file ...` / `@@READ ...` 这些标记是机器指令，
    # 留在叙述里会让"可见回复"变成一串路径加空白 —— 聊天界面上第一眼看过去就是这样。
    # 它们本来也不该进压缩器的语义抽取（进度、失败、遗留问题都靠散文）。
    pieces: list[str] = []
    cursor = 0
    for start, end in spans:
        pieces.append(o_i[cursor:start])
        cursor = end
    pieces.append(o_i[cursor:] if o_i else "")

    prose: list[str] = []
    for line in "\n".join(pieces).splitlines():
        stripped = line.strip()
        if MARKER_LINE_RE.match(stripped):
            continue
        prose.append(line)
    # 折叠掉剥块之后留下的大片空行，但保留段落分隔
    narrative = re.sub(r"\n{3,}", "\n\n", "\n".join(prose)).strip()
    out.narrative = narrative

    # 重名文件（同一轮写了两次）：后者覆盖前者，但要记一笔
    seen: dict[str, int] = {}
    for f in out.files:
        seen[f.path] = seen.get(f.path, 0) + 1
    for path, n in seen.items():
        if n > 1:
            out.errors.append(f"同一轮重复写入 {path}（{n} 次，后者生效）")

    # "没落盘"算不算错，取决于这一轮**该不该落盘**。
    #   实验路径（require_files=True，缺省）：每轮都在推进项目，空手而归 = 解析错误。
    #   对话路径（chat_driven，require_files=False）：提问、闲聊、解释本来就不该动文件 ——
    #      那条规则会把每一次正常对话都判成 parse_output **硬违反**、再回退工作区
    #      （真机实测：一句"先别动文件，说说你记得什么"就被判 revert）。
    if require_files and not out.files and o_i and o_i.strip():
        out.errors.append("O_i 里没有任何 ```file: 块 —— 本轮无产物落盘")

    return out


def extract_prior_heads(state, head_chars: int = 60) -> tuple[list[str], list[str]]:
    """给压缩器的 C_{i-1} 结构头部（id + 一行标签），不是散文。"""
    nodes = [
        f"{f.id} {f.statement[:head_chars]}"
        + (f" | 锚 {f.anchor.key()}" if f.anchor else "")
        for f in state.active_facts()
    ]
    fails = [f"{x.id} 试{x.attempt}" for x in state.failure_chain]
    return nodes, fails


UNIT_RE = re.compile(
    r"^[ \t]*@@UNIT[ \t]+(?P<kind>file|validate|note)[ \t]*(?P<target>.*?)[ \t]*$",
    re.MULTILINE,
)


def parse_declared_checkpoints(o_i: str) -> list[Checkpoint]:
    """解析工作模型**自己声明**的工作单位边界。

        @@UNIT file pkg/video.py
        @@UNIT validate
        @@UNIT note 对比了 zip 与 tar 的容器开销

    这些是 declared 来源，可信度低。server 之后会拿真实状态变化去标注 backed：
      - file 声明 vs 本轮实际写入的文件（比 sha256）
      - validate 声明 -> 恒有据（server 每轮都跑验证器）
      - note -> 不可验证，backed=None
    """
    out: list[Checkpoint] = []
    for m in UNIT_RE.finditer(o_i or ""):
        kind = m.group("kind")
        target = " ".join(m.group("target").split())
        if kind == "file" and not target:
            continue  # `@@UNIT file` 不带路径是空声明，直接丢掉
        if kind == "note" and not target:
            continue  # 空 note 没有信息量
        out.append(Checkpoint(kind=kind, target=target, source="declared"))  # type: ignore[arg-type]
    return out


READ_RE = re.compile(r"^[ \t]*@@READ[ \t]+(?P<path>[^\s`]+)[ \t]*$", re.MULTILINE)


def parse_read_requests(o_i: str) -> list[str]:
    """解析 G 组的磁盘检索申请（`@@READ 相对路径`）。

    只**申请**，不执行 —— 真正读盘由 server 做，这样：
      - 越界路径与不存在的路径都能被记账（无效检索率）
      - 读了哪些文件、多少字节完全可审计（检索的上下文成本）
      - 模型无法绕过 server 直接改文件

    **刻意保留重复项**：同一轮里把同一个路径要两遍，正是"没有记忆"的典型症状，
    它会在 `_serve_reads` 里被记成冗余检索。去重会把这类失败信号抹掉。
    """
    out: list[str] = []
    for m in READ_RE.finditer(o_i or ""):
        raw = m.group("path").strip().strip("`'\"")
        ok, _ = is_safe_relpath(raw)
        if not ok:
            continue
        out.append(raw)
    return out


def extract_json_object(text: str) -> str | None:
    """从模型输出里抠出第一个平衡的 JSON 对象（容忍前后包裹文字/代码围栏）。"""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        start = text.find("{", start + 1)
    return None
