"""画面上部の利用者表示の値（BR1.16、US1.5 AC1.5.1〜AC1.5.3）。"""

import pytest

from src.models.authz import Principal, Role
from web.user_badge import build_user_badge


def _principal(**overrides: object) -> Principal:
    params: dict[str, object] = {
        "sub": "user-1",
        "roles": frozenset({Role.MEMBER}),
        "group_ids": frozenset(),
        "display_name": "山田 花子",
        "email": "hanako@example.com",
    }
    params.update(overrides)
    return Principal(**params)  # type: ignore[arg-type]


def test_name_is_shown_when_present():
    assert build_user_badge(_principal()).name == "山田 花子"


def test_email_is_shown_when_there_is_no_name():
    badge = build_user_badge(_principal(display_name=None))
    assert badge.name == "hanako@example.com"


@pytest.mark.parametrize(
    ("roles", "label"),
    [
        (frozenset({Role.ADMIN, Role.MEMBER}), "admin"),
        (frozenset({Role.ADMIN}), "admin"),
        (frozenset({Role.MEMBER}), "member"),
        (frozenset(), "ロールなし"),
    ],
)
def test_role_label(roles, label):
    assert build_user_badge(_principal(roles=roles)).role_label == label


def test_no_department():
    badge = build_user_badge(_principal(group_ids=frozenset()))
    assert badge.departments_label == "所属グループなし"
    assert badge.hidden_count == 0


@pytest.mark.parametrize(
    ("groups", "label"),
    [
        (frozenset({"企画部"}), "企画部"),
        (frozenset({"企画部", "マーケティング部"}), "マーケティング部、企画部"),
    ],
)
def test_one_or_two_departments_are_all_shown(groups, label):
    badge = build_user_badge(_principal(group_ids=groups))
    assert badge.departments_label == label
    assert badge.hidden_count == 0


def test_three_or_more_departments_show_two_and_the_rest_count():
    badge = build_user_badge(
        _principal(
            group_ids=frozenset({"企画部", "マーケティング部", "人事部", "総務部"})
        )
    )
    assert badge.shown_departments == ("マーケティング部", "人事部")
    assert badge.hidden_count == 2
    assert badge.departments_label == "マーケティング部、人事部"
    assert badge.all_departments == ("マーケティング部", "人事部", "企画部", "総務部")


def test_no_principal_means_no_badge():
    assert build_user_badge(None) is None
