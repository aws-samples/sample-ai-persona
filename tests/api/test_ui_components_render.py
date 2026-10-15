"""Rendering of the U3 shared parts and page-level accessibility markers.

Behaviour (keys, focus, timers) is covered by ``tests/ui/``; these tests look at
the HTML the server renders (NFR6.7, NFR6.10, NFR6.11, contract C6).
"""

import re
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

_TEMPLATES = Path(__file__).parent.parent.parent / "web" / "templates"
_CLASS_ATTR = re.compile(r'class="([^"]*)"')


def _outline_removed_without_replacement(html: str) -> list[str]:
    """Class lists that drop the outline without a ring or focus-visible style."""
    offenders = []
    for match in _CLASS_ATTR.finditer(html):
        classes = match.group(1).split()
        if "focus:outline-none" not in classes:
            continue
        if any(c.startswith(("focus:ring-", "focus-visible:")) for c in classes):
            continue
        offenders.append(match.group(1))
    return offenders


class TestFocusIsNeverHidden:
    """NFR6.7: `focus:outline-none` always comes with a visible replacement."""

    def test_templates_keep_a_visible_focus_style(self) -> None:
        offenders = {
            str(path.relative_to(_TEMPLATES)): found
            for path in _TEMPLATES.rglob("*.html")
            if (found := _outline_removed_without_replacement(path.read_text("utf-8")))
        }
        assert offenders == {}

    def test_check_detects_a_hidden_focus(self) -> None:
        html = '<input class="border focus:outline-none"><a class="focus:outline-none focus:ring-2">'
        assert _outline_removed_without_replacement(html) == [
            "border focus:outline-none"
        ]


def _render(source: str, **context: object) -> str:
    """Render a template string with the app's Jinja environment."""
    from web.main import templates

    return str(templates.env.from_string(source).render(**context))


def _tag(html: str, pattern: str) -> str:
    match = re.search(rf"<[^>]*{pattern}[^>]*>", html)
    assert match, f"{pattern} を持つ要素がない"
    return match.group(0)


class TestDialogMacro:
    """Contract C6: `dialog()` renders an accessible, initially hidden dialog."""

    _SOURCE = (
        '{% from "components/dialog.html" import dialog, dialog_open_button %}'
        "{{ dialog_open_button('share-dialog', '共有') }}"
        "{% call dialog('share-dialog', '共有先の設定', describedby=note) %}"
        '<p id="share-notice">注意</p>{% endcall %}'
    )

    def test_dialog_has_role_modal_label_and_starts_hidden(self) -> None:
        html = _render(self._SOURCE, note="share-notice")

        outer = _tag(html, 'id="share-dialog"')
        assert re.search(r"\shidden(\s|>|=)", outer), outer
        assert "data-dialog" in outer
        panel = _tag(html, 'role="dialog"')
        assert 'aria-modal="true"' in panel
        assert 'aria-labelledby="share-dialog-title"' in panel
        assert 'aria-describedby="share-notice"' in panel
        heading = _tag(html, 'id="share-dialog-title"')
        assert heading.startswith("<h2")
        assert "共有先の設定" in html

    def test_describedby_is_omitted_when_not_given(self) -> None:
        html = _render(self._SOURCE, note=None)

        assert "aria-describedby" not in _tag(html, 'role="dialog"')

    def test_close_button_is_labelled(self) -> None:
        html = _render(self._SOURCE, note=None)

        close = _tag(html, 'data-testid="share-dialog-close-button"')
        assert 'aria-label="閉じる"' in close
        assert "data-dialog-close" in close

    def test_open_button_points_at_the_dialog(self) -> None:
        html = _render(self._SOURCE, note=None)

        button = _tag(html, 'data-dialog-open="share-dialog"')
        assert 'aria-haspopup="dialog"' in button
        assert 'type="button"' in button

    def test_optional_flags_become_markers(self) -> None:
        html = _render(
            '{% from "components/dialog.html" import dialog %}'
            "{{ dialog('d', 't', content_url='/x', close_on_backdrop=True, autoopen=True,"
            " remove_on_close=True, close_on_success=True) }}"
        )

        outer = _tag(html, 'id="d"')
        for marker in (
            "data-dialog-backdrop-close",
            "data-dialog-autoopen",
            "data-dialog-remove-on-close",
            "data-dialog-close-on-success",
        ):
            assert marker in outer
        content = _tag(html, "data-dialog-content")
        assert 'hx-get="/x"' in content
        assert 'hx-trigger="dialogLoad"' in content


class TestListTabsMacro:
    """Contract C6: `list_tabs()` / `list_tabpanel()` only display given values."""

    _SOURCE = (
        '{% from "components/list_tabs.html" import list_tabs, list_tabpanel %}'
        "{{ list_tabs(active, counts, show_all, '/persona/management', '#persona-list') }}"
        "{% call list_tabpanel('persona-list', active, base_url='/persona/management') %}"
        "{% endcall %}"
    )

    def test_selected_tab_and_panel(self) -> None:
        html = _render(
            self._SOURCE,
            active="shared",
            counts={"mine": 2, "shared": 1},
            show_all=False,
        )

        selected = _tag(html, 'data-tab="shared"')
        assert 'aria-selected="true"' in selected
        assert 'tabindex="0"' in selected
        assert 'hx-push-url="true"' in selected
        assert 'href="/persona/management?tab=shared"' in selected
        assert 'hx-target="#persona-list"' in selected
        assert 'aria-selected="false"' in _tag(html, 'data-tab="mine"')
        assert 'data-tab="all"' not in html
        panel = _tag(html, 'role="tabpanel"')
        assert 'aria-labelledby="persona-list-tab-shared"' in panel
        assert 'data-reload-url="/persona/management?tab=shared"' in panel

    def test_missing_count_is_a_dash(self) -> None:
        html = _render(
            self._SOURCE,
            active="mine",
            counts={"mine": None, "shared": 0, "all": 9},
            show_all=True,
        )

        assert 'data-count-label="自分のもの -"' in html
        assert 'data-count-label="共有されたもの 0 件"' in html
        assert 'data-count-label="すべて 9 件"' in html


class TestBulkSelectMacro:
    """Contract C6: nothing is rendered on the 「共有されたもの」 tab."""

    _SOURCE = (
        '{% from "components/bulk_select.html" import bulk_select_bar, bulk_select_row %}'
        "{{ bulk_select_bar('persona', selectable, dialog_id='share-dialog') }}"
        "{{ bulk_select_row('p1', '佐藤 太郎', selectable) }}"
    )

    def test_not_selectable_renders_nothing(self) -> None:
        assert _render(self._SOURCE, selectable=False).strip() == ""

    def test_selectable_renders_bar_and_labelled_row(self) -> None:
        html = _render(self._SOURCE, selectable=True)

        assert 'aria-label="佐藤 太郎 を選択"' in _tag(html, "data-bulk-row")
        action = _tag(html, "data-bulk-action")
        assert "disabled" in action
        assert 'data-dialog-open="share-dialog"' in action
        assert "0 件を選択中" in html


class TestDepartmentPickerMacro:
    """Contract C6: `department_picker()` reads Department and keeps ids unique."""

    _SOURCE = (
        '{% from "components/department_picker.html" import department_picker %}'
        "{{ department_picker('departments', choices, selected, mixed, id_prefix='bulk') }}"
        "{{ department_picker('departments', choices, selected, mixed, id_prefix='single') }}"
    )

    @staticmethod
    def _context() -> dict[str, object]:
        from src.models.authz import Department

        return {
            "choices": [
                Department(group_id="sales", display_name="営業部"),
                Department(group_id="plan", display_name="企画部"),
            ],
            "selected": frozenset({"sales"}),
            "mixed": {"plan": (1, 3)},
        }

    def test_two_pickers_with_the_same_name_have_unique_ids(self) -> None:
        html = _render(self._SOURCE, **self._context())

        ids = re.findall(r'\sid="([^"]+)"', html)
        assert ids, "id がない"
        assert len(ids) == len(set(ids)), sorted(ids)
        for prefix in ("bulk", "single"):
            assert f'aria-labelledby="{prefix}-confirm-text"' in html
            assert f'aria-describedby="{prefix}-plan-mixed"' in html

    def test_id_prefix_defaults_to_the_name(self) -> None:
        html = _render(
            '{% from "components/department_picker.html" import department_picker %}'
            "{{ department_picker('departments', choices, selected, mixed) }}",
            **self._context(),
        )

        assert 'id="departments-confirm-text"' in html
        assert 'id="departments-plan-mixed"' in html
        assert 'value="sales"' in _tag(html, 'value="sales"')
        assert "checked" in _tag(html, 'value="sales"')


class TestStatusLabelMacro:
    """NFR6.11 / A3: the state is text in 〔〕 with a border, not colour alone."""

    _SOURCE = '{% from "components/status_label.html" import status_label %}{{ status_label(text) }}'

    def test_text_is_wrapped_in_brackets_with_a_border(self) -> None:
        html = _render(self._SOURCE, text="参照権限なし")

        label = _tag(html, "data-status-label")
        assert "〔参照権限なし〕" in html
        assert "border" in label.split('class="')[1].split()

    def test_only_neutral_text_colour_is_used(self) -> None:
        html = _render(self._SOURCE, text="削除済み")

        classes = (
            _tag(html, "data-status-label").split('class="')[1].split('"')[0].split()
        )
        colours = [
            c
            for c in classes
            if c.startswith(("text-", "bg-")) and not c.startswith("text-xs")
        ]
        assert colours == ["text-gray-700"]

    def test_text_is_escaped(self) -> None:
        html = _render(self._SOURCE, text="<b>x</b>")

        assert "<b>" not in html
        assert "〔&lt;b&gt;x&lt;/b&gt;〕" in html


def _list_screen_managers() -> dict[str, Mock]:
    from src.models.sharing import ListTab, TabbedList

    discussion = Mock()
    # U6: 議論の一覧もタブ別（TabbedList）
    discussion.list_discussions.return_value = TabbedList(
        tab=ListTab.MINE, items=[], counts={"mine": 0, "shared": 0}
    )
    execution = Mock()
    execution.get_all_surveys.return_value = []
    template = Mock()
    template.get_all_templates.return_value = []
    return {
        "web.routers.discussion.get_discussion_manager": discussion,
        "web.routers.survey.get_execution_manager": execution,
        "web.routers.survey.get_template_manager": template,
    }


def _title(html: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
    assert match, "title がない"
    return " ".join(match.group(1).split())


class TestPageTitles:
    """NFR6.10 / A7: the four list screens and M11 have distinct titles."""

    def test_titles_are_distinct(self, test_app, login_as) -> None:
        paths = [
            "/persona/management",
            "/discussion/results",
            "/survey/results",
            "/survey/templates",
            "/no-such-page",
        ]
        patches = [patch(t, return_value=m) for t, m in _list_screen_managers().items()]
        for p in patches:
            p.start()
        try:
            with TestClient(test_app, follow_redirects=False) as browser:
                login_as(browser, roles=("admin",))
                titles = [
                    _title(browser.get(p, headers={"accept": "text/html"}).text)
                    for p in paths
                ]
        finally:
            for p in patches:
                p.stop()

        assert len(set(titles)) == len(titles), titles
        assert titles[-1] == "ページが見つかりません - AIペルソナシステム"
