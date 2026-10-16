"""Python 源码的 AST 工具。

被压缩器和验证器共用 —— 这一点很重要：**结构性事实（接口、依赖、文档）
不靠模型抽取，靠 AST 抽取**。模型只负责语义性内容（进度、被否方案、遗留问题）。

好处是双重的：
  - 结构性事实零幻觉，锚点天然可溯源；
  - C 里的依赖图与真实代码依赖的一致性，是"算出来"的而不是"说出来的"。
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SymbolInfo:
    name: str
    kind: str  # func / async_func / class / method
    signature: str
    has_docstring: bool
    lineno: int

    @property
    def is_public(self) -> bool:
        return not self.name.startswith("_")


def parse_source(src: str) -> ast.Module | None:
    try:
        return ast.parse(src)
    except SyntaxError:
        return None


def parse_file(path: Path) -> ast.Module | None:
    try:
        return parse_source(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def path_to_module(rel_path: str, layer_root: str = "") -> str:
    """`drawvid/drawing.py` -> `drawvid.drawing`；`drawvid/core/__init__.py` -> `drawvid.core`。"""
    p = Path(rel_path)
    parts = list(p.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _args_src(args: ast.arguments) -> str:
    """把 ast.arguments 还原成与源码等价的参数串（含默认值）。"""
    out: list[str] = []
    posonly = list(getattr(args, "posonlyargs", []) or [])
    positional = posonly + list(args.args)
    defaults = list(args.defaults)
    pad = len(positional) - len(defaults)

    for i, a in enumerate(positional):
        piece = a.arg
        if a.annotation is not None:
            piece += f": {ast.unparse(a.annotation)}"
        if i >= pad:
            piece += f" = {ast.unparse(defaults[i - pad])}"
        out.append(piece)

    if args.vararg is not None:
        piece = "*" + args.vararg.arg
        if args.vararg.annotation is not None:
            piece += f": {ast.unparse(args.vararg.annotation)}"
        out.append(piece)
    elif args.kwonlyargs:
        out.append("*")

    for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        piece = a.arg
        if a.annotation is not None:
            piece += f": {ast.unparse(a.annotation)}"
        if d is not None:
            piece += f" = {ast.unparse(d)}"
        out.append(piece)

    if args.kwarg is not None:
        piece = "**" + args.kwarg.arg
        if args.kwarg.annotation is not None:
            piece += f": {ast.unparse(args.kwarg.annotation)}"
        out.append(piece)

    return ", ".join(out)


def canonical_signature(node: ast.AST) -> str:
    """规范化签名，用于与 P 的契约做逐字比对。"""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return ""
    name = node.name
    args = _args_src(node.args)
    sig = f"{name}({args})"
    if node.returns is not None:
        sig += f" -> {ast.unparse(node.returns)}"
    return normalize_signature(sig)


def normalize_signature(sig: str) -> str:
    """抹平空白差异，其余保持逐字。"""
    return " ".join(sig.split())


def has_docstring(node: ast.AST) -> bool:
    body = getattr(node, "body", None) or []
    if not body:
        return False
    first = body[0]
    return (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    )


def symbols_of(tree: ast.Module, *, include_methods: bool = True) -> list[SymbolInfo]:
    """模块内所有函数/类（含方法）。"""
    out: list[SymbolInfo] = []

    def visit(node: ast.AST, in_class: bool) -> None:
        for child in getattr(node, "body", []) or []:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "async_func" if isinstance(child, ast.AsyncFunctionDef) else "func"
                if in_class:
                    kind = "method"
                out.append(
                    SymbolInfo(
                        name=child.name,
                        kind=kind,
                        signature=canonical_signature(child),
                        has_docstring=has_docstring(child),
                        lineno=child.lineno,
                    )
                )
                if include_methods:
                    visit(child, in_class)
            elif isinstance(child, ast.ClassDef):
                out.append(
                    SymbolInfo(
                        name=child.name,
                        kind="class",
                        signature="",
                        has_docstring=has_docstring(child),
                        lineno=child.lineno,
                    )
                )
                visit(child, True)
            elif isinstance(child, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                visit(child, in_class)

    visit(tree, False)
    return out


def module_level_public_defs(tree: ast.Module) -> list[SymbolInfo]:
    """只看**模块顶层**公开符号（契约、docstring、孤儿判定都以此为作用域）。

    注意排除方法：方法不是"模块的公开接口"，把它们算进来会让事实链里塞满
    `to_svg` / `append` 之类的成员，C 的 token 预算会被无谓地挤爆。
    """
    return [
        s
        for s in symbols_of(tree, include_methods=False)
        if s.is_public and s.kind != "method"
    ]


def import_targets(tree: ast.Module, current_module: str) -> list[str]:
    """本模块依赖的模块名（相对 import 会解析成绝对模块名）。

    `__future__` 不是依赖，滤掉 —— 否则它会在依赖图和分层检查里冒充一个真实模块。
    """
    pkg_parts = current_module.split(".")[:-1]
    out: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                # level=1 指当前包；level=2 指上一层
                cut = len(pkg_parts) - (node.level - 1)
                base = pkg_parts[: max(0, cut)]
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            if mod and mod != "__future__":
                out.add(mod)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name != "__future__":
                    out.add(alias.name)
    return sorted(out)


def module_level_names(tree: ast.Module) -> set[str]:
    """模块顶层**所有**绑定名：函数 / 类 / 赋值 / 注解赋值。

    与 `module_level_public_defs` 的分工是本质的：那个回答"这个模块的公开接口是什么"
    （契约、docstring 覆盖用），只认函数与类；这个回答"这个名字在文件里存不存在"
    （锚点解析用）—— `TOOLS_DIR = Path(__file__).resolve().parent` 不是接口，
    但它确实是文件里的符号。拿前者去做后者，会把真实存在的常量判成"符号不存在"、
    渲染成 `[未落地]`，下一轮据此以为文件不对、白找一轮（真机会话 `5914fb8d00184172`）。
    """
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def find_test_functions(tree: ast.Module) -> list[str]:
    """模块内所有 `test_*` 函数名。

    命名注意：**不要**把这个函数叫 `test_*` —— 测试模块 `from ... import` 它之后，
    pytest 会把它当成本模块的测试去收集，然后索要一个不存在的 fixture。
    """
    return [
        s.name
        for s in symbols_of(tree, include_methods=False)
        if s.name.startswith("test_")
    ]


def called_names(tree: ast.Module) -> set[str]:
    """被调用的名字集合（含属性访问的最后一段，够用于"符号是否被测试触达"）。"""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                out.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                out.add(fn.attr)
    return out


def referenced_names(tree: ast.Module) -> set[str]:
    """所有被引用到的标识符名（比 called_names 宽，用于孤儿符号判定）。"""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                out.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                out.add((alias.asname or alias.name).split(".")[0])
    return out


def iter_py_files(root: Path, skip: Iterable[str] = ()) -> list[Path]:
    skip = tuple(skip)
    out: list[Path] = []
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root).as_posix()
        if any(part in skip for part in rel.split("/")):
            continue
        out.append(p)
    return out
