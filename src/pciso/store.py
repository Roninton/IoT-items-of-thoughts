"""会话持久化：append-only JSONL + 幂等轮次 + 快照（支撑 revert）。

为什么不是可选项：5 组 × 8 轮真实 API 调用，中途一定会挂。
不能续跑就得从头重跑，这是纯浪费。

布局::

    state/
      sessions/<sid>/
        session.json      元数据（config / prefix_hash / 状态）
        prefix.md         冻结的 P 正文（便于事后判读）
        rounds.jsonl      append-only，每行一个已提交轮次（轨迹日志 = 一等公民）
        events.jsonl      append-only，非轮次事件（revert / error / dry_run）
        chat.jsonl        append-only，对话窗口里说过的话（**不是循环的一部分**）
        thinking/<i>.txt  第 i 轮的工作模型 thinking 原文（评估提纯率用，不进 git）
        snapshots/<i>.zip 第 i 轮**之前**的工作区快照，revert 用
      workspaces/<sid>/   真实项目树
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import time
import uuid
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .models import ArtifactIndex


def new_session_id() -> str:
    return uuid.uuid4().hex[:16]


class GitCommitError(RuntimeError):
    """逐轮提交失败。宁可停下，也不要产出"看起来正常但没有溯源"的轨迹。"""


class SessionStore:
    def __init__(
        self,
        state_dir: str | Path,
        session_id: str,
        workspace: str | Path | None = None,
    ):
        self.state_dir = Path(state_dir).resolve()
        self.session_id = session_id
        self.root = self.state_dir / "sessions" / session_id
        # workspace 可指向外部仓库（真实实验里 = ../drawing-video-toolchain），
        # 这样"被生成的项目"自带 git 历史，而轨迹日志留在 server 仓库里。
        self.workspace_override = Path(workspace).resolve() if workspace else None
        self.workspace = self.workspace_override or (
            self.state_dir / "workspaces" / session_id
        )
        self.snapshot_dir = self.root / "snapshots"
        self._ensure()

    # -- 生命周期 --------------------------------------------------------- #

    def _ensure(self) -> None:
        for d in (self.root, self.snapshot_dir, self.workspace):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def meta_path(self) -> Path:
        return self.root / "session.json"

    @property
    def rounds_path(self) -> Path:
        return self.root / "rounds.jsonl"

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    def exists(self) -> bool:
        return self.meta_path.exists()

    def write_meta(self, meta: dict[str, Any]) -> None:
        meta = dict(meta)
        meta["session_id"] = self.session_id
        meta["updated_at"] = time.time()
        self.meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def read_meta(self) -> dict[str, Any]:
        if not self.meta_path.exists():
            raise KeyError(f"session {self.session_id} 不存在")
        return json.loads(self.meta_path.read_text(encoding="utf-8"))

    def patch_meta(self, **kw: Any) -> dict[str, Any]:
        meta = self.read_meta()
        meta.update(kw)
        self.write_meta(meta)
        return meta

    def delete(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.workspace, ignore_errors=True)

    # -- 轮次（append-only + 幂等） --------------------------------------- #

    def read_rounds(self) -> list[dict[str, Any]]:
        if not self.rounds_path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self.rounds_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # 半截行 = 上次崩在写入中间：跳过，不污染状态
                continue
        return out

    def round_index(self) -> int:
        return len(self.read_rounds())

    def append_round(self, record: dict[str, Any]) -> None:
        with self.rounds_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()

    def append_event(self, kind: str, **payload: Any) -> None:
        rec = {"kind": kind, "ts": time.time(), **payload}
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()

    def iter_events(self) -> Iterator[dict[str, Any]]:
        if not self.events_path.exists():
            return iter(())
        lines = self.events_path.read_text(encoding="utf-8").splitlines()
        return iter([json.loads(x) for x in lines if x.strip()])

    def last_round(self) -> dict[str, Any] | None:
        rounds = self.read_rounds()
        return rounds[-1] if rounds else None

    # -- 对话窗口的显示记录 ----------------------------------------------- #

    @property
    def chat_path(self) -> Path:
        """对话窗口说过的那些话。

        与 `rounds.jsonl` 的区别是本质的：**它不是循环的一部分** ——
        不产生 ΔC、不喂回模型、不参与判定、C 不前进。写它只有一个理由：
        "已经渲染出来的东西是不可变的" —— 聊天窗口刷新之后，说过的话原样还在，
        而且永远是当时的原文，不会被后来任何一次压缩改写。
        """
        return self.root / "chat.jsonl"

    def read_chat(self) -> list[dict[str, Any]]:
        if not self.chat_path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self.chat_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 半截行 = 上次崩在写入中间，跳过
        return out

    def append_chat(self, record: dict[str, Any]) -> None:
        rec = {"ts": time.time(), **record}
        with self.chat_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()

    # -- 每轮的 thinking 原文 ---------------------------------------------- #

    @property
    def thinking_dir(self) -> Path:
        """每轮的 thinking 原文，一轮一个文件（`thinking/000.txt`，序号与快照一致、0 起）。

        它是**评估"提纯率"的唯一原料**：只记 `thinking_chars` 这个数字的话，
        "提纯出来的几条节点够不够用"就只能靠模型自己回忆 —— 而回忆不是证据。
        与 chat.jsonl 同属运行数据：体积大（真机单轮 6~11K 字），不进 git，
        也不是交付物（交付物是 rounds.jsonl）。
        """
        return self.root / "thinking"

    def write_thinking(self, index: int, text: str) -> Path:
        self.thinking_dir.mkdir(parents=True, exist_ok=True)
        path = self.thinking_dir / f"{index:03d}.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def read_thinking(self, index: int) -> str:
        path = self.thinking_dir / f"{index:03d}.txt"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    # -- 快照 ------------------------------------------------------------- #

    def snapshot_path(self, index: int) -> Path:
        return self.snapshot_dir / f"{index:03d}.zip"

    SNAPSHOT_SKIP = (".git/", "__pycache__/", ".pytest_cache/", ".ruff_cache/")

    def _managed_files(self) -> list[Path]:
        out: list[Path] = []
        for p in sorted(self.workspace.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(self.workspace).as_posix()
            if any(rel.startswith(s) or f"/{s}" in f"/{rel}" for s in self.SNAPSHOT_SKIP):
                continue
            out.append(p)
        return out

    def snapshot(self, index: int) -> Path:
        """把当前工作区打包成"第 index 轮之前"的状态（排除 .git）。"""
        out = self.snapshot_path(index)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in self._managed_files():
                zf.write(p, p.relative_to(self.workspace).as_posix())
        return out

    def restore(self, index: int) -> list[str]:
        """回退工作区到第 index 轮之前。**不碰 .git**，返回被还原的文件列表。"""
        snap = self.snapshot_path(index)
        if not snap.exists():
            return []
        restored: list[str] = []
        for p in self._managed_files():
            # 只读文件或已被并发删除时不该中断整个回退
            with contextlib.suppress(OSError):
                p.unlink()
        with zipfile.ZipFile(snap) as zf:
            for name in zf.namelist():
                zf.extract(name, self.workspace)
                restored.append(name)

        # 工具缓存不是"被生成的项目"的一部分，但它们会让"回退 = 完全没发生"不成立。
        # 验证器每轮都会跑 ruff / pytest，于是回退后工作区里会多出 `.ruff_cache/`。
        # 属性测试把这个抓出来了（24 个种子里有 9 次"回退后工作区没还原"）。
        # 这些缓存是内容键控的，通常无害 —— 但"通常"正是这类静默问题的温床：
        # 回退语义要么字面为真，要么它迟早会在某处骗到人。
        for junk in self.workspace.rglob("__pycache__"):
            shutil.rmtree(junk, ignore_errors=True)
        for name in (".ruff_cache", ".pytest_cache"):
            shutil.rmtree(self.workspace / name, ignore_errors=True)
        return restored

    # -- git 集成（让被生成项目自带可归因的历史） ------------------------- #

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.workspace), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,  # 调用方检查 returncode，不靠异常传递
        )

    def git_available(self) -> bool:
        if not (self.workspace / ".git").exists():
            return False
        return self._git("rev-parse", "--is-inside-work-tree").returncode == 0

    def git_ensure_identity(self) -> bool:
        """确保仓库里有提交者身份。

        **必须在已有仓库上也做。** 原实现在 `git_init` 里只在"自己新建的仓库"上设置身份，
        已有仓库直接 early return —— 而本机全局 git 身份是空的，
        于是每一次 `git commit` 都以 exit 128 失败。
        这个坑是靠"七组 x 8 轮零提交"才发现的。
        """
        if self._git("config", "user.name").stdout.strip():
            return True
        self._git("config", "user.name", "pciso-experiment")
        self._git("config", "user.email", "pciso@localhost")
        return bool(self._git("config", "user.name").stdout.strip())

    def git_init(self) -> bool:
        self.workspace.mkdir(parents=True, exist_ok=True)
        if not self.git_available():
            self._git("init", "-q", "-b", "main")
        self.git_ensure_identity()
        return self.git_available()

    def git_commit(
        self, message: str, trailers: dict[str, Any] | None = None
    ) -> str | None:
        """按轮提交。trailer 让"实验生成"与"实验后人工加固"边界机器可查。

        提交失败**必须响亮**。原来它在失败时只 `return None`，于是七组 x 8 轮
        一个提交都没产生、却没有任何报错 —— 而缺提交会让下一组的分支复位带上
        上一组的 staged 内容，七组对照静默失效。静默失败在这里的代价是整场实验。
        """
        if not self.git_available():
            return None
        self.git_ensure_identity()
        self._git("add", "-A")
        if not self._git("status", "--porcelain").stdout.strip():
            return None
        body = self.format_commit_message(message, trailers)
        res = self._git("commit", "-q", "-m", body)
        if res.returncode != 0:
            detail = (res.stderr or res.stdout).strip().splitlines()
            raise GitCommitError(
                "git 提交失败 —— 实验的逐轮溯源会断，必须停下来处理："
                + (detail[-1] if detail else f"exit {res.returncode}")
            )
        return self._git("rev-parse", "HEAD").stdout.strip() or None

    @staticmethod
    def format_commit_message(message: str, trailers: dict[str, Any] | None = None) -> str:
        """正文与 trailer 块之间**必须空一行**。

        git 只把"最后一段、且与正文用空行隔开"的 Key: value 当作 trailer。
        实测：紧挨着写时 `%(trailers:key=PCISO-Round,valueonly)` 返回空字符串
        （而 `--grep` 仍然能搜到 —— 所以只测 --grep 会漏掉这条）。
        trailer 一旦解析不出来，"逐轮可溯源"就只是文本上的巧合，
        任何按 trailer 取值的工具（含 scripts/verify_rehearsal.py）都会看到空。
        """
        if not trailers:
            return message
        block = "\n".join(f"{k}: {v}" for k, v in trailers.items())
        return f"{message}\n\n{block}"

    def git_head(self) -> str | None:
        if not self.git_available():
            return None
        return self._git("rev-parse", "HEAD").stdout.strip() or None

    def git_prepare_arm_branch(self, branch: str, base: str = "main") -> dict[str, Any]:
        """把工作区切到该组专属的分支，并复位到 base 的干净状态。

        **为什么必须这样做**：七组对照共用同一个目标仓库。如果都在一条分支上往下写，
        B 组会从 A 组的成品接着做、C 组再继承 B 组 —— 七组就不再是可比的对照，
        而是同一条流水线的七个阶段。那种数据看起来完全正常，结论却一条都不成立。

        做法：`checkout -B <branch> <base>`，再 `reset --hard <base>` **把索引也一起复位**，
        最后 `clean -fd` 清掉未跟踪文件。

        `reset --hard` 是必需的：`clean` 只清未跟踪文件，**不碰已 staged 的**。
        原先只有 checkout + clean，于是当上一组的提交失败、`git add -A` 把整份成品
        留在索引里时，下一组的 checkout 会把它**带过去** ——
        实测里 C~G 五组就这样跑在了 B 组的成品之上，第 1 轮违反数从 19 变成 0。
        分支隔离形同虚设，而报告看起来完美无缺。
        """
        info: dict[str, Any] = {"branch": branch, "base": base, "ok": False, "cleaned": False}
        if not self.git_available():
            info["reason"] = "工作区不是 git 仓库"
            return info
        self.git_ensure_identity()

        if self._git("rev-parse", "--verify", "--quiet", base).returncode != 0:
            # 基线不存在（空仓库 / 还没提交过任何东西）。
            #
            # 这里**什么都不做**，尤其不能 clean。教训：原先的实现在这种情况下退化成了
            # "就地建分支 + clean -fd"，而空仓库里所有文件都是未跟踪的 ——
            # 于是 clean 把整个工作区删空了。真机上交付仓库的脚手架是已提交的所以没炸，
            # 但这是纯粹靠运气。宁可什么都不做，也不要删掉别人没提交的东西。
            info["base"] = "(unborn)"
            info["reason"] = (
                f"基线分支 {base!r} 不存在（仓库里可能还没有任何提交）："
                f"跳过分支切换与清理，避免删掉未提交的内容"
            )
            info["head"] = self.git_head()
            return info

        res = self._git("checkout", "-q", "-B", branch, base)
        info["checkout_rc"] = res.returncode
        if res.returncode != 0:
            info["reason"] = (res.stderr or res.stdout).strip()[:200]
            return info

        # 索引与工作树一起复位到基线 —— 这一步是"每组从同一基线出发"的实际保证。
        self._git("reset", "-q", "--hard", base)
        untracked = [
            line[3:]
            for line in self._git("status", "--porcelain").stdout.splitlines()
            if line.startswith("??")
        ]
        info["untracked_before_clean"] = len(untracked)
        self._git("clean", "-qfd")

        # 复位之后必须真的是干净的；不干净就明确失败，而不是带着污染往下跑。
        residue = [
            line for line in self._git("status", "--porcelain").stdout.splitlines() if line.strip()
        ]
        info["residue"] = residue[:20]
        if residue:
            info["reason"] = (
                f"复位后工作区仍有 {len(residue)} 项未清理：{residue[:3]} —— "
                f"这一组会在被污染的状态上开跑，拒绝继续"
            )
            return info
        info["cleaned"] = True
        info["head"] = self.git_head()
        info["ok"] = True
        return info

    # -- 产物 ------------------------------------------------------------- #

    def write_file(self, rel_path: str, content: str) -> Path:
        target = (self.workspace / rel_path).resolve()
        ws = self.workspace.resolve()
        if ws not in target.parents and target != ws:
            raise ValueError(f"越界写入被拒: {rel_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def index_artifacts(self) -> ArtifactIndex:
        import hashlib

        files: list[str] = []
        hashes: dict[str, str] = {}
        for p in sorted(self.workspace.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(self.workspace).as_posix()
            if "__pycache__" in rel or rel.endswith(".pyc"):
                continue
            files.append(rel)
            hashes[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        tests = [
            f
            for f in files
            if f.startswith(("tests/", "test_")) or "/test_" in f
        ]
        docs = [f for f in files if f.lower().endswith((".md", ".rst")) or "/docs/" in f]
        return ArtifactIndex(files=files, tests=tests, docs=docs, hashes=hashes)
