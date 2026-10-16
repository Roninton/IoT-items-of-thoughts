"""O_i 的解析：文件块、路径安全、以及 @@UNIT 检查点声明。

`@@UNIT` 是**低可信**信号（模型自己说的），所以这里的测试重点是：
它能被正确解析，并且 server 能把它和可验证的状态变化对照出"无据声明"。
"""

from __future__ import annotations

from pciso.oparse import (
    extract_json_object,
    is_safe_relpath,
    parse_declared_checkpoints,
    parse_o_i,
)

O_I = '''## 第 2 轮

先说设计取舍。

@@UNIT file pkg/a.py

```file:pkg/a.py
"""A。"""


def f(a: int) -> int:
    """f。"""
    return a
```

@@UNIT validate
@@UNIT note 对比过 zip 与 tar

```file:tests/test_a.py
"""测试。"""


def test_f():
    """用例。"""
    assert True
```
'''


def test_parse_files_and_narrative() -> None:
    parsed = parse_o_i(O_I)
    assert parsed.paths() == ["pkg/a.py", "tests/test_a.py"]
    assert "设计取舍" in parsed.narrative
    assert "def f" not in parsed.narrative  # 文件块内容不算叙述
    assert parsed.errors == []


def test_narrative_excludes_protocol_markers() -> None:
    """`@@UNIT` / `@@READ` 是给 server 的指令，不该混进"可见回复"。

    只剥 file 块会让叙述变成一串路径加空白 —— 聊天界面上第一眼就是那个样子。
    """
    parsed = parse_o_i(O_I)
    assert "@@UNIT" not in parsed.narrative
    assert "@@" not in parsed.narrative
    assert "设计取舍" in parsed.narrative


def test_narrative_collapses_blank_gaps_left_by_file_blocks() -> None:
    parsed = parse_o_i(O_I)
    assert "\n\n\n" not in parsed.narrative
    assert not parsed.narrative.startswith("\n")
    assert not parsed.narrative.endswith("\n")


def test_read_markers_are_stripped_too() -> None:
    parsed = parse_o_i("先看一眼。\n\n@@READ pkg/a.py\n\n```file:pkg/a.py\nx = 1\n```\n")
    assert parsed.narrative == "先看一眼。"


def test_file_content_is_preserved_fully() -> None:
    parsed = parse_o_i(O_I)
    a = next(f for f in parsed.files if f.path == "pkg/a.py")
    assert '"""A。"""' in a.content
    assert a.content.endswith("\n")
    assert a.sha256 and a.bytes > 0


def test_path_traversal_is_rejected() -> None:
    ok, why = is_safe_relpath("../etc/passwd")
    assert not ok and "越界" in why
    ok, why = is_safe_relpath("/abs/path.py")
    assert not ok and "绝对" in why
    ok, _ = is_safe_relpath(".git/config")
    assert not ok


def test_unsafe_block_produces_error_not_a_write() -> None:
    parsed = parse_o_i("```file:../evil.py\nx = 1\n```\n")
    assert parsed.files == []
    assert any("越界" in e for e in parsed.errors)


def test_no_file_blocks_is_an_error() -> None:
    parsed = parse_o_i("这一轮我只想了想，没有落盘。")
    assert parsed.files == []
    assert any("没有任何" in e for e in parsed.errors)


def test_no_file_blocks_is_fine_when_the_round_is_conversational() -> None:
    """对话轮次（require_files=False）：不落盘不是错。

    真机实测：一句"先别动文件，说说你记得什么"会让 parse_output 变成**硬违反**、
    整轮判 revert —— 那等于给每一次正常对话都盖一个"回退"的章。
    """
    parsed = parse_o_i("这一轮我只想了想，没有落盘。", require_files=False)
    assert parsed.errors == []
    assert parsed.narrative


def test_duplicate_write_is_noted_but_last_wins() -> None:
    parsed = parse_o_i("```file:pkg/a.py\nA = 1\n```\n\n```file:pkg/a.py\nA = 2\n```\n")
    assert len(parsed.files) == 2
    assert any("重复写入" in e for e in parsed.errors)


# --------------------------------------------------------------------------- #
# @@UNIT
# --------------------------------------------------------------------------- #


def test_declared_checkpoints_are_parsed() -> None:
    cps = parse_declared_checkpoints(O_I)
    assert [(c.kind, c.target) for c in cps] == [
        ("file", "pkg/a.py"),
        ("validate", ""),
        ("note", "对比过 zip 与 tar"),
    ]
    assert all(c.source == "declared" for c in cps)


def test_file_unit_without_target_is_dropped() -> None:
    assert parse_declared_checkpoints("@@UNIT file\n") == []


def test_note_unit_without_target_is_kept() -> None:
    """validate 无 target 要保留（server 每轮都跑验证器，它恒有据）。"""
    cps = parse_declared_checkpoints("@@UNIT validate\n")
    assert len(cps) == 1 and cps[0].kind == "validate" and cps[0].target == ""


def test_blank_note_is_dropped() -> None:
    assert parse_declared_checkpoints("@@UNIT note\n") == []


def test_no_units_yields_empty_list() -> None:
    assert parse_declared_checkpoints("没有任何标记。") == []


def test_units_do_not_leak_into_narrative() -> None:
    """标记行必须从叙述里剥掉：界面上的"可见回复"就是这段叙述。"""
    parsed = parse_o_i(O_I)
    assert "@@UNIT" not in parsed.narrative
    # 但检查点解析仍然能看到它们（那是从原始 o_i 解析的，不是从叙述）
    assert len(parse_declared_checkpoints(O_I)) == 3


# --------------------------------------------------------------------------- #
# JSON 抽取
# --------------------------------------------------------------------------- #


def test_extract_json_from_fenced_block() -> None:
    assert extract_json_object('前言\n```json\n{"ops": []}\n```\n后记') == '{"ops": []}'


def test_extract_json_handles_nested_braces_and_strings() -> None:
    text = 'xx {"a": {"b": "}"}, "c": 1} yy'
    got = extract_json_object(text)
    assert got is not None
    import json

    assert json.loads(got) == {"a": {"b": "}"}, "c": 1}


def test_extract_json_returns_none_when_absent() -> None:
    assert extract_json_object("没有 JSON") is None
