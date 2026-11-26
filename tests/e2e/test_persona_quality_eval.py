"""Evaluation of the personas an advanced generation writes.

Not a score gate: the cases run through the running app against the dev
stack (as ``test_method_selection_eval.py``) and what they gave is measured and
printed, so a change can be shown as numbers:

- invented numbers: numbers a persona states that the analysis never showed;
- distinctness: 1 minus the overlap between the personas' texts;
- the judge model's scores (1〜5) on five criteria (``persona_quality.py``).

A case that cannot be measured fails the test (the generation failed, no
persona came out, or the judge's reply held no scores). The scores themselves
are not gated: one run is too noisy (the generation and the judge both vary),
so they are compared by hand between changes.

    uv run pytest tests/e2e/test_persona_quality_eval.py -m e2e -s
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from playwright.sync_api import BrowserContext, Page

from tests.e2e.persona_quality import (
    CRITERIA,
    JudgeScoreError,
    JudgeScores,
    distinctness,
    invented_numbers,
    judge,
    judge_prompt,
)
from tests.e2e.test_advanced_generation import (
    CUSTOMERS_CSV,
    GENERATION_TIMEOUT_MS,
    PLAN_TIMEOUT_MS,
    PURCHASES_CSV,
    _open_advanced_page,
    _open_section,
    _restore_section,
    _save_section,
    _section_state,
    _upload_named,
)


@dataclass(frozen=True)
class Case:
    name: str
    files: tuple[Path, ...]
    purpose: str


CASES = (
    Case(
        "rfm",
        (CUSTOMERS_CSV, PURCHASES_CSV),
        "購入の新しさ・頻度・金額で、優良なお客さまと離れそうなお客さまを分けたい",
    ),
    Case(
        "compare", (CUSTOMERS_CSV,), "ゴールドとベーシックの会員で年齢の違いを比べたい"
    ),
)


@dataclass
class Measured:
    case: Case
    segments_text: str = ""
    personas: list[str] = field(default_factory=list)
    invented: list[list[str]] = field(default_factory=list)
    distinct: float = 0.0
    scores: JudgeScores | None = None
    failed: str = ""


def _run_case(page: Page, case: Case, run_tag: str) -> Measured:
    measured = Measured(case)
    _open_advanced_page(page)
    for path in case.files:
        _upload_named(page, path)
    page.get_by_test_id("advanced-purpose").fill(f"{run_tag} {case.purpose}")
    page.get_by_test_id("advanced-persona-count").select_option("2")
    page.get_by_test_id("advanced-confirm-plan").check()
    page.get_by_test_id("advanced-generate-button").click()
    page.locator(
        '[data-testid="advanced-plan-approve"], '
        '[data-testid="advanced-failure"]:not([hidden])'
    ).first.wait_for(timeout=PLAN_TIMEOUT_MS)
    failure = page.get_by_test_id("advanced-failure")
    if failure.is_visible():
        measured.failed = failure.inner_text()[:200]
        return measured
    page.get_by_test_id("advanced-plan-approve").click()
    page.locator(
        '[data-testid="advanced-result"], [data-testid="advanced-failure"]:not([hidden])'
    ).first.wait_for(timeout=GENERATION_TIMEOUT_MS)
    if failure.is_visible():
        measured.failed = failure.inner_text()[:200]
        return measured
    result = page.get_by_test_id("advanced-result")
    measured.segments_text = result.get_by_test_id("advanced-segments").inner_text()
    for card in result.get_by_test_id("advanced-candidate").all():
        measured.personas.append(card.inner_text())
    return measured


def _measure(measured: Measured) -> None:
    for text in measured.personas:
        age = re.search(r"（(\d+)）", text)
        allowed = (age.group(1),) if age else ()
        measured.invented.append(
            invented_numbers(text, measured.segments_text, allowed=allowed)
        )
    measured.distinct = distinctness(measured.personas)
    try:
        measured.scores = judge(
            judge_prompt(
                purpose=measured.case.purpose,
                segments_text=measured.segments_text,
                personas_text="\n\n---\n\n".join(measured.personas),
            )
        )
    except JudgeScoreError as e:
        measured.failed = f"judge: {e}"


def test_persona_quality_with_the_real_models(
    session_context: BrowserContext, run_tag: str
) -> None:
    page = session_context.new_page()
    form = _open_section(page)
    before = _section_state(form)
    results: list[Measured] = []
    try:
        switch = form.get_by_test_id("advanced-setting-detailed")
        if switch.get_attribute("aria-checked") != "true":
            switch.click()
            _save_section(page, form)
        for case in CASES:
            measured = _run_case(page, case, run_tag)
            if not measured.failed:
                _measure(measured)
            results.append(measured)
    finally:
        _restore_section(page, before)
        page.close()

    unmeasured = [
        f"{m.case.name}: {m.failed or 'no persona'}"
        for m in results
        if m.failed or not m.personas or m.scores is None
    ]
    print("\n[U6 eval] persona quality")
    for m in results:
        if m.scores is None:
            print(f"  {m.case.name}: failed={m.failed or 'not measured'}")
            continue
        scores = " ".join(f"{c}={m.scores.values[c]}" for c in CRITERIA)
        print(
            f"  {m.case.name}: judge_mean={m.scores.mean:.1f} {scores} "
            f"distinct={m.distinct:.2f} "
            f"invented_numbers={[len(i) for i in m.invented]} "
            f"comment={m.scores.comment}"
        )
    if unmeasured:
        pytest.fail("cases that could not be measured: " + "; ".join(unmeasured))
