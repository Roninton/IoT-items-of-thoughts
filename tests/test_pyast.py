"""pyast：结构性事实抽取的基础（契约比对、依赖图、文档约束都靠它）。"""

from __future__ import annotations

import ast

from pciso.pyast import (
    find_test_functions,
    import_targets,
    module_level_public_defs,
    parse_source,
    path_to_module,
    symbols_of,
)

SRC = '''
"""模块文档。"""

from __future__ import annotations

from .core import Point


def line(x0: int, y0: int, x1: int, y1: int) -> list[Point]:
    """直线。"""
    return []


def _private() -> None:
    pass


class Canvas:
    """画布。"""

    def to_svg(self) -> str:
        """导出。"""
        return ""

    def _hidden(self) -> None:
        pass


def no_docstring(x: int) -> int:
    return x
'''


def test_path_to_module() -> None:
    assert path_to_module("drawvid/drawing.py") == "drawvid.drawing"
    assert path_to_module("drawvid/core/__init__.py") == "drawvid.core"


def test_signature_is_ast_canonical() -> None:
    tree = parse_source(SRC)
    assert tree is not None
    sigs = {s.name: s.signature for s in symbols_of(tree, include_methods=True)}
    assert sigs["line"] == "line(x0: int, y0: int, x1: int, y1: int) -> list[Point]"
    assert sigs["to_svg"] == "to_svg(self) -> str"
    assert sigs["no_docstring"] == "no_docstring(x: int) -> int"


def test_module_level_public_defs_excludes_methods_and_privates() -> None:
    tree = parse_source(SRC)
    assert tree is not None
    names = {s.name for s in module_level_public_defs(tree)}
    assert names == {"line", "Canvas", "no_docstring"}
    assert "to_svg" not in names
    assert "_private" not in names


def test_docstring_detection() -> None:
    tree = parse_source(SRC)
    assert tree is not None
    doc = {s.name: s.has_docstring for s in module_level_public_defs(tree)}
    assert doc["line"] is True
    assert doc["Canvas"] is True
    assert doc["no_docstring"] is False


def test_relative_import_resolves_to_absolute() -> None:
    tree = parse_source(SRC)
    assert tree is not None
    assert import_targets(tree, "drawvid.drawing") == ["drawvid.core"]


def test_import_level_two() -> None:
    tree = parse_source("from ..core import x\n")
    assert tree is not None
    assert import_targets(tree, "drawvid.sub.mod") == ["drawvid.core"]


def test_find_test_functions() -> None:
    tree = parse_source("def test_a():\n    pass\n\n\ndef helper():\n    pass\n")
    assert tree is not None
    assert find_test_functions(tree) == ["test_a"]


def test_syntax_error_returns_none() -> None:
    assert parse_source("def broken(:\n") is None


def test_defaults_render_in_signature() -> None:
    tree = ast.parse("def f(a: int, b: str = 'x', *args: int, **kw: str) -> None: ...\n")
    sig = symbols_of(tree, include_methods=False)[0].signature
    assert sig == "f(a: int, b: str = 'x', *args: int, **kw: str) -> None"
