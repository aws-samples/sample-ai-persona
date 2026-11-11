"""移すリソースの種別の選択（--include / --exclude）のテスト（移行スクリプト）。"""

import pytest

from scripts.v0_migration.selection import KINDS, SelectionError, select_kinds


def test_default_is_every_kind():
    assert select_kinds(None, None) == KINDS


def test_include_keeps_the_dependency_order():
    assert select_kinds(["discussions", "users", "personas"], None) == (
        "users",
        "personas",
        "discussions",
    )


def test_exclude():
    selected = select_kinds(None, ["users", "knowledge_bases"])
    assert "users" not in selected
    assert "knowledge_bases" not in selected
    assert "personas" in selected


def test_include_and_exclude_together():
    assert select_kinds(["personas", "bindings"], ["bindings"]) == ("personas",)


@pytest.mark.parametrize(
    "include, exclude", [(["persona"], None), (None, ["nemotron"])]
)
def test_unknown_kind(include, exclude):
    with pytest.raises(SelectionError, match="unknown"):
        select_kinds(include, exclude)


def test_nothing_selected():
    with pytest.raises(SelectionError, match="nothing"):
        select_kinds(["users"], ["users"])
