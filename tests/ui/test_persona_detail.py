"""Persona detail (U5, M8, BR3.5, BR4.2, NFR6.16).

The detail header and body are rendered as the owner sees them and as a shared
reader sees them. The binding lists are loaded by htmx from canned responses
rendered with the real partial templates.
"""

from dataclasses import replace

import pytest

from src.managers.persona_manager import BindingRow, DatasetBindingsView, KbBindingView
from src.models.persona import Persona
from tests.ui.conftest import UiHarness, render_page

PERSONA = replace(
    Persona.create_new(
        name="佐藤 太郎",
        age=34,
        occupation="会社員",
        background="東京で働いている。",
        values=["効率"],
        pain_points=["時間"],
        goals=["成長"],
    ),
    id="p1",
)
ORG_DATASET = BindingRow(target_id="ds-org", name="組織の購買データ", usable=False)
DEDICATED = BindingRow(
    target_id="ds-own",
    name="行動データ",
    usable=True,
    dedicated=True,
    binding_keys={"user_id": "U1"},
)
KB = BindingRow(target_id="kb1", name="社内KB", usable=False)


def _bindings(manageable: bool) -> tuple[str, str]:
    datasets = render_page(
        "persona/partials/dataset_bindings.html",
        persona_id=PERSONA.id,
        view=DatasetBindingsView(
            persona_id=PERSONA.id,
            manageable=manageable,
            bindings=[DEDICATED, ORG_DATASET],
        ),
    )
    kb = render_page(
        "persona/partials/kb_binding.html",
        persona_id=PERSONA.id,
        view=KbBindingView(persona_id=PERSONA.id, manageable=manageable, binding=KB),
    )
    return datasets, kb


def _open(ui: UiHarness, *, manageable: bool, shared: bool = True) -> None:
    datasets, kb = _bindings(manageable)
    ui.respond(f"/persona/{PERSONA.id}/dataset-bindings", body=datasets)
    ui.respond(f"/persona/{PERSONA.id}/kb-binding", body=kb)
    from web.routers import persona as persona_router

    # 詳細の表示ヘルパー（アバターの URL など）はペルソナの Router の環境が持つ
    helpers = {
        name: persona_router.templates.env.globals[name]
        for name in ("persona_avatar_url", "gender_label", "country_name")
    }
    ui.load_component(
        "persona_detail.html",
        **helpers,
        persona=PERSONA,
        manageable=manageable,
        owner_label="自分" if manageable else "中村 由美",
        shared_with=["営業部"] if shared else [],
        avatar_upload_enabled=True,
    )
    ui.page.wait_for_selector("[data-testid='dataset-binding-row']", state="attached")
    ui.page.wait_for_selector("[data-testid='kb-binding-row']", state="attached")
    ui.settle()


def _count(ui: UiHarness, selector: str) -> int:
    return int(ui.page.locator(selector).count())


def test_shared_reader_has_no_memory_binding_changes_or_avatar_change(
    ui: UiHarness,
) -> None:
    _open(ui, manageable=False)

    assert _count(ui, "#memory-section-content") == 0
    assert _count(ui, "[hx-post*='dataset-bindings'], [hx-delete*='bindings']") == 0
    assert _count(ui, "[hx-post*='kb-binding']") == 0
    assert _count(ui, "form[hx-post$='/avatar']") == 0
    assert _count(ui, "[data-testid='persona-delete-button']") == 0
    # 見るだけの操作（エクスポート）は出す
    assert _count(ui, "[data-testid='persona-export-button']") == 1


def test_owner_has_memory_binding_changes_and_avatar_change(ui: UiHarness) -> None:
    _open(ui, manageable=True)

    assert _count(ui, "#memory-section-content") == 1
    assert _count(ui, "[hx-delete='/persona/p1/dataset-bindings/ds-own']") == 1
    assert _count(ui, "[hx-delete='/persona/p1/kb-binding']") == 1
    assert _count(ui, "form[hx-post$='/avatar']") == 1


def test_export_button_is_pressable_with_the_keyboard(ui: UiHarness) -> None:
    ui.respond(
        f"/persona/{PERSONA.id}/export",
        body="# 佐藤 太郎\n",
        headers={
            "Content-Type": "text/markdown; charset=utf-8",
            "Content-Disposition": 'attachment; filename="persona.md"',
        },
    )
    _open(ui, manageable=False)

    ui.page.focus("[data-testid='persona-export-button']")
    assert ui.active("document.activeElement.dataset.testid") == (
        "persona-export-button"
    )
    with ui.page.expect_download() as download:
        ui.page.keyboard.press("Enter")
    # ファイル名（Content-Disposition）は API のテストが確かめる
    assert download.value.url.endswith(f"/persona/{PERSONA.id}/export")


def _delete_dialog_text(ui: UiHarness) -> str:
    return str(ui.page.inner_text("#persona-delete-dialog [role=dialog]"))


def test_delete_confirmation_names_what_goes_and_focuses_cancel(
    ui: UiHarness,
) -> None:
    _open(ui, manageable=True)

    ui.page.click("[data-testid='persona-delete-button']")

    text = _delete_dialog_text(ui)
    assert "「佐藤 太郎」を削除します" in text
    assert "データセット・ナレッジベースの紐付け" in text
    assert "このペルソナの長期記憶" in text
    assert "共有先のグループからも見えなくなります" in text
    # 取り消せない操作なので、既定のフォーカスは［キャンセル］
    assert ui.active("document.activeElement.dataset.testid") == (
        "persona-delete-cancel"
    )
    labelled = ui.page.get_attribute(
        "#persona-delete-dialog [role=dialog]", "aria-describedby"
    )
    assert labelled == "persona-delete-dialog-description"


def test_delete_confirmation_of_an_unshared_persona(ui: UiHarness) -> None:
    _open(ui, manageable=True, shared=False)

    ui.page.click("[data-testid='persona-delete-button']")

    assert "共有先のグループからも見えなくなります" not in _delete_dialog_text(ui)


def test_cancel_closes_without_deleting(ui: UiHarness) -> None:
    _open(ui, manageable=True)
    ui.page.click("[data-testid='persona-delete-button']")

    ui.page.keyboard.press("Enter")  # フォーカスは［キャンセル］

    hidden = ui.page.evaluate(
        "() => document.getElementById('persona-delete-dialog').hidden"
    )
    assert hidden is True
    # 削除の要求は出ていない（出れば応答の表にない要求としても失敗する）
    assert "/persona/p1" not in ui.requests


def test_no_permission_is_text_not_colour(ui: UiHarness) -> None:
    _open(ui, manageable=True)

    labels = ui.page.locator(
        "[data-testid='dataset-binding-row'] [data-status-label],"
        " [data-testid='kb-binding-row'] [data-status-label]"
    ).all_inner_texts()
    assert labels.count("〔利用権限なし〕") == 2
    assert "〔このペルソナ専用〕" in labels
    # 使えない組織資産にはプレビューを出さない
    assert _count(ui, "[data-dialog-open='dataset-preview-ds-org']") == 0
    assert _count(ui, "[data-dialog-open='dataset-preview-ds-own']") == 1


@pytest.mark.parametrize("manageable", [True, False], ids=["owner", "shared"])
def test_detail_has_no_axe_violations(ui: UiHarness, manageable: bool) -> None:
    _open(ui, manageable=manageable)
    ui.check_a11y()


def test_delete_dialog_has_no_axe_violations(ui: UiHarness) -> None:
    _open(ui, manageable=True)
    ui.page.click("[data-testid='persona-delete-button']")
    ui.check_a11y()
