"""把本地 git 历史整体推到 GitHub —— 走 api.github.com 的 Git Data API。

**为什么不用 `git push`**（本机实测）：
  · `github.com` = 20.205.243.166，TCP 443 时通时断；`git push` 大概率直接
    `Failed to connect to github.com:443` 或 `Recv failure: Connection was reset`，
    28MB 的包不可能在这种链路上一次传完。
  · SSH 路线也不通：`%USERPROFILE%\\.ssh` 里没有私钥，token 的 scope 只有
    `gist, repo, workflow`（没有 `admin:public_key`），注册不了新公钥。
  · 只有 **api.github.com**（20.205.243.168）稳定可达 ⇒ 用 Git Data API 逐对象重建：
    blob → tree → commit → ref。**ref 最后一步才建**，所以中途失败不会留下半个分支，
    重跑只需补对象（同内容对象的 sha 由内容决定，重复创建返回同一个 sha）。

用法：
    python scripts/_push_via_api.py --owner Roninton --repo IoT-items-of-thoughts
    python scripts/_push_via_api.py --limit 5          # 只推最近 5 个提交（历史截断）
    python scripts/_push_via_api.py --dry-run          # 只统计要传多少对象，不发请求

凭据：`git credential fill`（Git Credential Manager 里已存的 github.com token）。
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK，中文进度会炸

BASE = "https://api.github.com"
UA = "pciso-git-data-push"


# --------------------------------------------------------------------------- 凭据 / git


def token_from_helper() -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    proc = subprocess.run(
        ["git", "-c", "credential.interactive=false", "credential", "fill"],
        input=b"protocol=https\nhost=github.com\n\n",
        capture_output=True,
        env=env,
        check=False,
    )
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("拿不到 GitHub 凭据：git credential fill 没返回 password")


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{proc.stderr.decode('utf-8', 'replace')}")
    return proc.stdout.decode("utf-8", "replace")


class CatFile:
    """常驻 `git cat-file --batch`：读对象比每次起进程快两个数量级。"""

    def __init__(self, repo: Path) -> None:
        self.proc = subprocess.Popen(
            ["git", "-C", str(repo), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        self.lock = threading.Lock()

    def read(self, sha: str) -> bytes:
        with self.lock:
            assert self.proc.stdin and self.proc.stdout
            self.proc.stdin.write((sha + "\n").encode())
            self.proc.stdin.flush()
            header = self.proc.stdout.readline().split()
            if len(header) < 3:
                raise SystemExit(f"cat-file 读不到 {sha}：{header!r}")
            size = int(header[2])
            data = self.proc.stdout.read(size)
            self.proc.stdout.read(1)  # 尾随换行
        return data


# --------------------------------------------------------------------------- HTTP


class Api:
    def __init__(self, token: str, owner: str, repo: str, workers: int) -> None:
        self.token = token
        self.slug = f"/repos/{owner}/{repo}"
        self.workers = workers
        self.posts = 0
        self.lock = threading.Lock()

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload).encode() if payload is not None else None
        for attempt in range(1, 8):
            req = urllib.request.Request(
                BASE + path,
                data=body,
                method=method,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "Content-Type": "application/json",
                    "User-Agent": UA,
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    raw = resp.read()
                with self.lock:
                    self.posts += 1
                return json.loads(raw.decode()) if raw else {}
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")[:300]
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if exc.code in (403, 429, 500, 502, 503, 504) and attempt < 7:
                    wait = float(retry_after) if retry_after else min(60.0, 2.0**attempt)
                    print(f"    [{exc.code}] 退避 {wait:.0f}s（{text.splitlines()[0][:110]}）", flush=True)
                    time.sleep(wait)
                    continue
                raise SystemExit(f"{method} {path} -> {exc.code}: {text}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < 7:
                    time.sleep(min(30.0, 2.0**attempt))
                    continue
                raise SystemExit(f"{method} {path} 网络失败：{exc}") from exc
        raise SystemExit("unreachable")

    def blob(self, data: bytes) -> str:
        out = self.request(
            "POST",
            f"{self.slug}/git/blobs",
            {"content": base64.b64encode(data).decode(), "encoding": "base64"},
        )
        return out["sha"]

    def tree(self, entries: list[dict]) -> str:
        out = self.request("POST", f"{self.slug}/git/trees", {"tree": entries})
        return out["sha"]

    def commit(self, payload: dict) -> str:
        out = self.request("POST", f"{self.slug}/git/commits", payload)
        return out["sha"]

    def try_get(self, path: str) -> dict | None:
        """GET，404 返回 None —— 用于「这条 ref 还不存在」这种正常分支。"""
        req = urllib.request.Request(
            BASE + path,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": UA,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise SystemExit(
                f"GET {path} -> {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}"
            ) from exc


# --------------------------------------------------------------------------- 对象收集


def parse_commit(raw: bytes) -> dict:
    text = raw.decode("utf-8", "replace")
    head, _, message = text.partition("\n\n")
    info: dict = {"tree": "", "parents": [], "author": None, "committer": None, "message": message}
    for line in head.splitlines():
        if line.startswith("tree "):
            info["tree"] = line[5:].strip()
        elif line.startswith("parent "):
            info["parents"].append(line[7:].strip())
        elif line.startswith(("author ", "committer ")):
            who, _, rest = line.partition(" ")
            name, _, tail = rest.partition(" <")
            email, _, when = tail.partition("> ")
            stamp, _, tz = when.strip().rpartition(" ")
            side = {"name": name.strip(), "email": email.strip(), "date": iso_date(stamp, tz)}
            if who == "author":
                info["author"] = side
            else:
                info["committer"] = side
    return info


def iso_date(stamp: str, tz: str) -> str:
    """把 `1700000000 +0800` 这种 `--date=raw` 输出转成同偏移的 ISO8601 字符串。

    时区偏移必须原样保留：作者行里的 epoch+偏移决定 commit 对象的 sha，
    用 UTC 改写会让远端 sha 与本地对不上（之后 push 就成了两套历史）。
    """
    m = re.match(r"([+-])(\d{2})(\d{2})$", tz.strip()) or re.match(r"([+-])(\d{2}):(\d{2})$", tz.strip())
    sign, hh, mm = ("+", "00", "00") if not m else (m.group(1), m.group(2), m.group(3))
    offset = (int(hh) * 60 + int(mm)) * (1 if sign == "+" else -1)
    local = time.gmtime(int(stamp) + offset * 60)
    return time.strftime("%Y-%m-%dT%H:%M:%S", local) + f"{sign}{hh}:{mm}"


def collect(repo: Path, branch: str, limit: int) -> tuple[list[str], dict, dict, set[str]]:
    shas = git(repo, "rev-list", "--reverse", branch).split()
    if limit:
        shas = shas[-limit:]
    cat = CatFile(repo)
    commits: dict[str, dict] = {}
    for sha in shas:
        commits[sha] = parse_commit(cat.read(sha))

    trees: dict[str, list] = {}
    blobs: set[str] = set()
    seen: set[str] = set()
    stack = [meta["tree"] for meta in commits.values()]
    while stack:
        tsha = stack.pop()
        if tsha in seen:
            continue
        seen.add(tsha)
        entries = []
        for line in git(repo, "ls-tree", tsha).splitlines():
            meta, _, name = line.partition("\t")
            mode, otype, osha = meta.split()
            entries.append((mode, otype, osha, name))
            if otype == "tree":
                stack.append(osha)
            else:
                blobs.add(osha)
        trees[tsha] = entries
    return shas, commits, trees, blobs


# --------------------------------------------------------------------------- 主流程


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--owner", default="Roninton")
    ap.add_argument("--repo", default="IoT-items-of-thoughts")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0, help="只推最近 N 个提交（历史截断）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = Path(args.repo_root).resolve()
    token = token_from_helper()
    shas, commits, trees, blob_set = collect(root, args.branch, args.limit)
    blobs = sorted(blob_set)
    print(f"对象统计：commit {len(shas)} / tree {len(trees)} / blob {len(blobs)}")
    if args.dry_run:
        return 0

    api = Api(token, args.owner, args.repo, args.workers)
    cat = CatFile(root)

    # 本地缓存：这个脚本可能要跑十几分钟，断了重跑不该把已经建好的对象再传一遍。
    cache_file = root / "out" / ".push_cache.json"
    cache: dict = {"blobs": {}, "trees": {}, "commits": {}}
    if cache_file.exists():
        with contextlib.suppress(OSError, ValueError):
            cache.update(json.loads(cache_file.read_text(encoding="utf-8")))

    def save_cache() -> None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache), encoding="utf-8")

    # 1) blob
    done = len([s for s in blobs if s in cache["blobs"]])
    todo = [s for s in blobs if s not in cache["blobs"]]
    failures: list[str] = []

    def push_blob(sha: str) -> None:
        nonlocal done
        try:
            data = cat.read(sha)
            remote = api.blob(data)
        except BaseException as exc:  # 线程里抛 SystemExit 会被吞掉，必须自己收
            failures.append(f"{sha[:8]}: {exc}")
            return
        if remote != sha:
            failures.append(f"blob {sha} -> {remote}（内容 sha 不一致）")
            return
        cache["blobs"][sha] = remote
        done += 1
        if done % 50 == 0 or done == len(blobs):
            print(f"  blob {done}/{len(blobs)}（POST 共 {api.posts}）", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(push_blob, todo))
    save_cache()
    if failures:
        raise SystemExit("blob 阶段有失败：\n  " + "\n  ".join(failures[:5]))
    print(f"blob 完成：{done}/{len(blobs)}（跳过已建 {len(blobs) - len(todo)}）", flush=True)

    # 2) tree（自底向上：先建子目录）
    made_trees: dict[str, str] = dict(cache["trees"])

    def ensure_tree(tsha: str) -> str:
        if tsha in made_trees:
            return made_trees[tsha]
        payload = []
        for mode, otype, osha, name in trees[tsha]:
            if otype == "tree":
                payload.append(
                    {"path": name, "mode": "040000", "type": "tree", "sha": ensure_tree(osha)}
                )
            else:
                payload.append({"path": name, "mode": mode, "type": "blob", "sha": osha})
        remote = api.tree(payload)
        made_trees[tsha] = remote
        cache["trees"][tsha] = remote
        if remote != tsha:
            print(f"    警告：tree {tsha} -> {remote}")
        if len(made_trees) % 100 == 0:
            print(f"  tree {len(made_trees)}/{len(trees)}", flush=True)
            save_cache()
        return remote

    for sha in shas:
        ensure_tree(commits[sha]["tree"])
    save_cache()
    print(f"tree 完成：{len(made_trees)}/{len(trees)}", flush=True)

    # 3) commit（按时间顺序，父提交先建）
    made_commits: dict[str, str] = dict(cache["commits"])
    for sha in shas:
        if sha in made_commits:
            continue
        meta = commits[sha]
        parents = [made_commits[p] for p in meta["parents"] if p in made_commits]
        payload = {
            "message": meta["message"],
            "tree": made_trees[meta["tree"]],
            "parents": parents,
        }
        if meta["author"]:
            payload["author"] = meta["author"]
        if meta["committer"]:
            payload["committer"] = meta["committer"]
        remote = api.commit(payload)
        made_commits[sha] = remote
        cache["commits"][sha] = remote
        if remote != sha:
            print(f"    警告：commit {sha[:8]} -> {remote[:8]}（作者/日期/父提交有差异）")
    save_cache()
    print(f"commit 完成：{len(made_commits)}", flush=True)

    # 4) ref（最后一步；失败也不会留下半个分支）
    tip = made_commits[shas[-1]]
    cur = api.try_get(f"{api.slug}/git/ref/heads/{args.branch}")
    if cur is None:
        api.request("POST", f"{api.slug}/git/refs", {"ref": f"refs/heads/{args.branch}", "sha": tip})
        print(f"ref 创建：{args.branch} -> {tip[:8]}")
    else:
        old = cur.get("object", {}).get("sha", "")
        api.request(
            "PATCH",
            f"{api.slug}/git/refs/heads/{args.branch}",
            {"sha": tip, "force": True},
        )
        print(f"ref 更新：{args.branch} {old[:8]} -> {tip[:8]}")
    print(f"完成：本地 {shas[-1][:8]} == 远端 {tip[:8]}？{shas[-1] == tip}（POST 共 {api.posts}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
