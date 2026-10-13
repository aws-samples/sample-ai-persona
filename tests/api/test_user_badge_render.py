"""画面上部に利用者表示とログアウトのフォームが出ること（US1.5、FR8.1、WF6）。"""

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
    # ロールのない利用者は U2 の BR2.8 でロール未割り当て画面へ移るため、ここでは
    # member で確かめる（「ロールなし」の表示は tests/unit/test_user_badge.py）。
    login_as(
        client, name=None, email="hanako@example.com", roles=("member",), groups=()
    )
    html = client.get("/persona/management").text

    mobile = _block(html, "user-badge-mobile")
    assert "hanako@example.com" in mobile
    assert "member" in mobile
    assert "所属部署なし" in mobile
    assert 'data-testid="signout-button-mobile"' in html


def test_signout_is_no_longer_a_link(client):
    html = client.get("/persona/management").text
    assert 'href="/signout"' not in html
