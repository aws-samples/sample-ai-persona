"""v0 のユーザーを v1 のプールへ作り直す計画のテスト（移行スクリプト）。

v1 に同じメールアドレスのユーザー（最初に作った admin など）がいれば、作成もグループの
変更もせずにスキップする。比べるときは前後の空白を除き、大文字と小文字を区別しない。
"""

from scripts.v0_migration.users import V0User, normalize_email, plan_users


def _user(username, email, enabled=True):
    return V0User(username=username, email=email, enabled=enabled)


def test_normalize_email():
    assert normalize_email("  Admin@Example.COM ") == "admin@example.com"
    assert normalize_email(None) == ""


def test_new_users_are_created():
    plan = plan_users([_user("u1", "a@example.com")], v1_emails=set())
    assert plan.create == ["a@example.com"]
    assert plan.existing == []
    assert plan.invalid == []


def test_existing_email_is_skipped_regardless_of_case():
    plan = plan_users(
        [_user("u1", "Admin@Example.com"), _user("u2", "b@example.com")],
        v1_emails={normalize_email("admin@example.COM ")},
    )
    assert plan.create == ["b@example.com"]
    assert plan.existing == ["admin@example.com"]


def test_missing_and_duplicate_emails_are_reported():
    plan = plan_users(
        [
            _user("u1", None),
            _user("u2", "dup@example.com"),
            _user("u3", "DUP@example.com"),
            _user("u4", "not-an-email"),
        ],
        v1_emails=set(),
    )
    assert plan.create == ["dup@example.com"]
    assert [(name, reason) for name, reason in plan.invalid] == [
        ("u1", "email_missing"),
        ("u3", "email_duplicate"),
        ("u4", "email_invalid"),
    ]


def test_disabled_users_are_not_recreated():
    plan = plan_users([_user("u1", "a@example.com", enabled=False)], v1_emails=set())
    assert plan.create == []
    assert plan.invalid == [("u1", "disabled")]
