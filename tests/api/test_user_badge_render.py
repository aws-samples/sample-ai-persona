"""The user badge and logout form appear at the top of the page."""

import re


def _block(html: str, testid: str) -> str:
    match = re.search(
        rf'<[^>]+data-testid="{testid}"[^>]*>(.*?)</(div|form)>', html, re.DOTALL
    )
    assert match, f"{testid} が画面にない"
    return match.group(0)


def test_header_shows_name_role_departments_and_signout(client, login_as):
    login_as(
        client,
        name="山田 花子",
        roles=("member",),
        groups=("企画部", "マーケティング部", "人事部"),
    )
    html = client.get("/persona/management").text

    badge = _block(html, "user-badge")
    assert "山田 花子" in badge
    assert "member" in badge
    assert "マーケティング部、人事部" in badge
    assert "ほか 1 件" in badge

    form = re.search(r'<form[^>]*action="/signout"[^>]*>', html)
    assert form and 'method="post"' in form.group(0)
    assert 'data-testid="signout-button"' in html


def test_mobile_menu_also_has_the_badge_and_signout(client, login_as):
    # A user without a role is redirected to the role-unassigned screen, so use
    # member here (the 'no role' display is tested in tests/unit/test_user_badge.py).
    login_as(
        client, name=None, email="hanako@example.com", roles=("member",), groups=()
    )
    html = client.get("/persona/management").text

    mobile = _block(html, "user-badge-mobile")
    assert "hanako@example.com" in mobile
    assert "member" in mobile
    assert "所属グループなし" in mobile
    assert 'data-testid="signout-button-mobile"' in html


def test_signout_is_no_longer_a_link(client):
    html = client.get("/persona/management").text
    assert 'href="/signout"' not in html


def test_member_navigation_has_no_settings_link(client, login_as):
    """The member's navigation (wide and narrow screens) has no 「設定」 entry."""
    login_as(client, roles=("member",), groups=("sales",))
    html = client.get("/persona/management").text
    assert 'href="/settings"' not in html


def test_admin_navigation_has_the_settings_link_twice(client, login_as):
    login_as(client, roles=("admin",), groups=())
    html = client.get("/persona/management").text
    assert html.count('href="/settings"') == 2
