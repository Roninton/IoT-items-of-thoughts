"""验收一个载体的脚本 oracle：把 8 个阶段叠起来，跑该载体的冻结契约测试。

     python scripts/verify_oracle.py                 # 验所有载体
    python scripts/verify_oracle.py one_sentence_edit
    python scripts/verify_oracle.py --keep          # 保留临时目录以便翻查

为什么要有这个脚本：新载体落地时，"oracle 是否真能通过冻结契约"是第一道闸门。
它同时也是**分数上限参照**的来源 —— 真实对照组的成绩如果低于 oracle，说明验证器在工作；
如果高于，说明尺子坏了。这个判断每次都要重跑，不能靠记忆。

顺带跑 ruff（--isolated，与 C8 的判定口径一致），因为 oracle 是"满分参照物"，
它自己 lint 不干净的话，C8 那条线的上限就是错的。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pciso.workmodels import STAGES_ROOT  # noqa: E402


def assemble(prefix_name: str, dest: Path) -> tuple[int, list[str]]:
    """把 stages/01..NN 依次叠加到 dest（后者覆盖前者）。返回阶段数与文件数。"""
    stages = STAGES_ROOT / prefix_name / "stages"
    if not stages.is_dir():
        raise FileNotFoundError(f"没有 oracle: {stages}")
    dirs = sorted(p for p in stages.iterdir() if p.is_dir())
    written: list[str] = []
    for stage in dirs:
        for path in sorted(stage.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(stage)
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.unlink()  # 后一阶段的同名文件是完整覆盖版，不是补丁
            shutil.copy2(path, target)
            written.append(rel.as_posix())
    return len(dirs), written


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(cwd)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=1800, env=env, check=False,
    )


def verify(prefix_name: str, *, keep: bool, run_ruff: bool) -> dict:
    prefix_dir = ROOT / "prefixes" / prefix_name
    frozen = prefix_dir / "tests_frozen"
    result: dict = {"prefix": prefix_name, "ok": False, "notes": []}
    if not frozen.is_dir():
        result["notes"].append(f"P 没有冻结测试目录: {frozen}")
        return result

    tmp = Path(tempfile.mkdtemp(prefix=f"pciso-oracle-{prefix_name}-"))
    try:
        n_stages, files = assemble(prefix_name, tmp)
        shutil.copytree(frozen, tmp / "tests" / "frozen")
        result["stages"] = n_stages
        result["files"] = len(set(files))

        proc = run(
            [sys.executable, "-m", "pytest", "tests", "-q", "--no-header",
             "-p", "no:cacheprovider"],
            tmp,
        )
        tail = (proc.stdout + proc.stderr).strip().splitlines()
        result["pytest_exit"] = proc.returncode
        result["pytest_tail"] = tail[-3:] if tail else []
        if proc.returncode != 0:
            result["notes"].append("冻结契约测试未全绿")
            return result

        if run_ruff:
            rproc = run(
                [sys.executable, "-m", "ruff", "check", "--isolated",
                 "--line-length", "100", "--exclude", "tests/frozen",
                 "--output-format", "concise", "."],
                tmp,
            )
            result["ruff_exit"] = rproc.returncode
            if rproc.returncode != 0:
                # 只要 `文件:行:列: 规则 说明` 这种真正的诊断行；
                # ruff 还会打印源码上下文行，那些不是错误本身。
                diags = [
                    x.strip()
                    for x in (rproc.stdout + rproc.stderr).splitlines()
                    if re.match(r"^\S+:\d+:\d+:", x.strip())
                ]
                result["ruff_findings"] = diags
                result["ruff_tail"] = diags[:8]
                result["notes"].append(f"ruff {len(diags)} 条（C8 的上限就不是满分）")

        # 多路输出对齐：这是新载体"一条时间轴多路输出"的核心性质
        demo = tmp / "demo.py"
        if demo.is_file():
            dproc = run([sys.executable, "demo.py"], tmp)
            result["demo_exit"] = dproc.returncode
            result["demo_tail"] = (dproc.stdout or "").strip().splitlines()[-2:]
            if dproc.returncode != 0:
                result["notes"].append("demo.py 跑不通")

        result["ok"] = result["pytest_exit"] == 0 and result.get("ruff_exit", 0) == 0
        return result
    finally:
        if keep:
            print(f"[keep] 临时目录: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="验收载体的脚本 oracle")
    ap.add_argument("prefixes", nargs="*", help="载体名（缺省=all）")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    ap.add_argument("--no-ruff", action="store_true", help="跳过 ruff")
    args = ap.parse_args(argv)

    names = args.prefixes or sorted(
        p.name for p in STAGES_ROOT.iterdir() if p.is_dir()
    )
    if not names:
        print("没有找到任何 oracle", file=sys.stderr)
        return 2

    failures = 0
    for name in names:
        print(f"=== {name} ===")
        try:
            res = verify(name, keep=args.keep, run_ruff=not args.no_ruff)
        except Exception as exc:
            print(f"  ERROR {type(exc).__name__}: {exc}")
            failures += 1
            continue
        print(f"  阶段 {res.get('stages')} / 文件 {res.get('files')}")
        for line in res.get("pytest_tail", []):
            print(f"  pytest: {line}")
        for line in res.get("demo_tail", []):
            print(f"  demo:   {line}")
        for line in res.get("ruff_tail", []):
            print(f"  ruff:   {line}")
        print(f"  => {'OK' if res['ok'] else 'FAIL: ' + '；'.join(res['notes'])}")
        if not res["ok"]:
            failures += 1
    print(f"\n{len(names) - failures}/{len(names)} 个载体通过")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
