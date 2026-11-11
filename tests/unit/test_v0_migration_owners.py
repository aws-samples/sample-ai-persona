"""対応表（リソース → 所有者・共有先・移すかどうか）と所有者の解決のテスト（移行スクリプト）。"""

import pytest

from scripts.v0_migration.owners import (
    MappingError,
    OwnerResolver,
    load_mapping,
)


def _csv(tmp_path, text):
    path = tmp_path / "mapping.csv"
    path.write_text(text, encoding="utf-8")
    return path


HEADER = "resource_type,resource_id,owner_email,group_ids,action\n"


class TestLoadMapping:
    def test_rows(self, tmp_path):
        mapping = load_mapping(
            _csv(
                tmp_path,
                HEADER
                + "persona,p1,A@example.com,sales;plan,\n"
                + "discussion,d1,,,skip\n"
                + "dataset,ds1,,sales,migrate\n",
            )
        )
        row = mapping.get("persona", "p1")
        assert row.owner_email == "a@example.com"
        assert row.group_ids == frozenset({"sales", "plan"})
        assert row.action == "migrate"
        assert mapping.skipped("discussion", "d1")
        assert not mapping.skipped("persona", "p1")
        assert mapping.get("dataset", "ds1").group_ids == frozenset({"sales"})
        assert mapping.get("persona", "unknown") is None
        assert mapping.group_ids() == frozenset({"sales", "plan"})

    def test_group_prefix_is_accepted(self, tmp_path):
        mapping = load_mapping(_csv(tmp_path, HEADER + "persona,p1,,group:sales,\n"))
        assert mapping.get("persona", "p1").group_ids == frozenset({"sales"})

    def test_optional_columns(self, tmp_path):
        mapping = load_mapping(
            _csv(tmp_path, "resource_type,resource_id\npersona,p1\n")
        )
        assert mapping.get("persona", "p1").action == "migrate"

    @pytest.mark.parametrize(
        "line, reason",
        [
            ("unknown,p1,,,\n", "resource_type"),
            ("persona,,,,\n", "resource_id"),
            ("persona,p1,,,delete\n", "action"),
            ("dataset,ds1,a@example.com,,\n", "owner"),
            ("persona,p1,,,\npersona,p1,,,\n", "duplicate"),
        ],
    )
    def test_invalid_rows(self, tmp_path, line, reason):
        with pytest.raises(MappingError, match=reason):
            load_mapping(_csv(tmp_path, HEADER + line))

    def test_missing_header(self, tmp_path):
        with pytest.raises(MappingError, match="resource_type"):
            load_mapping(_csv(tmp_path, "a,b\n1,2\n"))

    def test_empty_mapping(self):
        mapping = load_mapping(None)
        assert mapping.get("persona", "p1") is None
        assert mapping.group_ids() == frozenset()


class TestOwnerResolver:
    def test_default_and_mapped_owners(self, tmp_path):
        mapping = load_mapping(_csv(tmp_path, HEADER + "persona,p1,b@example.com,,\n"))
        resolver = OwnerResolver(
            {"a@example.com": "sub-a", "b@example.com": "sub-b"},
            default_email="A@example.com",
            mapping=mapping,
        )
        assert resolver.default_sub == "sub-a"
        assert resolver.owner("persona", "p1") == "sub-b"
        assert resolver.owner("persona", "p2") == "sub-a"

    def test_unknown_default_owner(self):
        with pytest.raises(MappingError, match="default owner"):
            OwnerResolver({}, default_email="a@example.com", mapping=load_mapping(None))

    def test_unknown_mapped_owner(self, tmp_path):
        mapping = load_mapping(_csv(tmp_path, HEADER + "persona,p1,x@example.com,,\n"))
        with pytest.raises(MappingError, match="x@example.com"):
            OwnerResolver(
                {"a@example.com": "sub-a"},
                default_email="a@example.com",
                mapping=mapping,
            )

    def test_planned_users_resolve_to_a_placeholder(self, tmp_path):
        mapping = load_mapping(
            _csv(tmp_path, HEADER + "persona,p1,new@example.com,,\n")
        )
        resolver = OwnerResolver(
            {"a@example.com": "sub-a"},
            default_email="a@example.com",
            mapping=mapping,
            planned_emails={"new@example.com"},
        )
        assert resolver.owner("persona", "p1") == "(new:new@example.com)"
