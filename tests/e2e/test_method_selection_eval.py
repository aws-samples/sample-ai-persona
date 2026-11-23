"""Evaluation of how the analysis picks its method with the real models (U6 NFR9.13).

Not a pass/fail test: four cases run through the running app against the dev
stack (as ``test_advanced_generation.py``), with the AI detailed analysis on,
and what was chosen is printed from the analysis record. The aim is three of
the four as expected; the result is written into ``code-summary.md`` by hand.

    uv run pytest tests/e2e/test_method_selection_eval.py -m e2e -s

Cases (made-up data in ``tests/e2e/data``):

1. join: customers and their purchases share ``customer_id`` -> ``join_tables``
2. compare_groups: customers, "compare the plans" -> a test between groups
3. rfm: customers and purchases, "good and drifting customers" -> ``rfm``
4. no common column: customers and the survey sample -> no join
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from playwright.sync_api import BrowserContext, Page, expect

from tests.e2e.conftest import DATA_DIR
from tests.e2e.test_advanced_generation import (
    CUSTOMERS_CSV,
    GENERATION_TIMEOUT_MS,
    PURCHASES_CSV,
    _open_advanced_page,
    _open_section,
    _restore_section,
    _save_section,
    _section_state,
    _upload_named,
)

SURVEY_CSV = DATA_DIR / "advanced_sample.csv"


@dataclass(frozen=True)
class Case:
    name: str
    files: tuple[Path, ...]
    purpose: str
    expected: str  # a word the record should hold, or "no-join"


CASES = (
    Case(
        "join",
        (CUSTOMERS_CSV, PURCHASES_CSV),
        "購入の多いお客さまの層を知りたい",
        "join_tables",
    ),
    Case(
        "compare_groups",
        (CUSTOMERS_CSV,),
        "ゴールドとベーシックの会員で年齢の違いを比べたい",
        "compare_groups",
    ),
    Case(
        "rfm",
        (CUSTOMERS_CSV, PURCHASES_CSV),
        "購入の新しさ・頻度・金額で、優良なお客さまと離れそうなお客さまを分けたい",
        "rfm",
    ),
    Case("no_common_column", (CUSTOMERS_CSV, SURVEY_CSV), "", "no-join"),
)


@dataclass
class Outcome:
    case: str
    expected: str
    kinds: list[str] = field(default_factory=list)
    code: str = ""
    joined: bool = False
    seconds: float = 0.0
    failed: str = ""

    @property
    def as_expected(self) -> bool:
        if self.failed:
            return False
        if self.expected == "no-join":
            return not self.joined
        return self.expected in self.code


def _run_case(page: Page, case: Case, run_tag: str) -> Outcome:
    outcome = Outcome(case.name, case.expected)
    _open_advanced_page(page)
    for path in case.files:
        _upload_named(page, path)
    page.get_by_test_id("advanced-purpose").fill(f"{run_tag} {case.purpose}".strip())
    page.get_by_test_id("advanced-persona-count").select_option("2")
    started = time.monotonic()
    page.get_by_test_id("advanced-generate-button").click()
    page.locator(
        '[data-testid="advanced-result"], [data-testid="advanced-failure"]:not([hidden])'
    ).first.wait_for(timeout=GENERATION_TIMEOUT_MS)
    outcome.seconds = time.monotonic() - started
    failure = page.get_by_test_id("advanced-failure")
    if failure.is_visible():
        outcome.failed = failure.inner_text()[:200]
        return outcome
    result = page.get_by_test_id("advanced-result")
    result.get_by_role("button", name="分析の記録").click()
    record = page.get_by_test_id("advanced-record")
    expect(record).to_be_visible()
    for summary in record.get_by_test_id("advanced-record-code-kind").all():
        outcome.kinds.append(summary.get_attribute("data-kind") or "")
    for block in record.get_by_test_id("advanced-record-code").all():
        block.locator("summary").click()
    outcome.code = "\n".join(record.locator("pre code").all_inner_texts())
    outcome.joined = record.get_by_test_id("advanced-record-joined").count() > 0
    page.keyboard.press("Escape")
    return outcome


def test_method_selection_with_the_real_models(
    session_context: BrowserContext, run_tag: str
) -> None:
    page = session_context.new_page()
    form = _open_section(page)
    before = _section_state(form)
    outcomes: list[Outcome] = []
    try:
        switch = form.get_by_test_id("advanced-setting-detailed")
        if switch.get_attribute("aria-checked") != "true":
            switch.click()
        _save_section(page, form)
        for case in CASES:
            outcomes.append(_run_case(page, case, run_tag))
    finally:
        _restore_section(page, before)
        page.close()

    print("\n[U6 eval] method selection (expected / AI code / joined / seconds)")
    for o in outcomes:
        print(
            f"  {o.case}: expected={o.expected} as_expected={o.as_expected} "
            f"ai_written={o.kinds.count('ai_written')} joined={o.joined} "
            f"seconds={o.seconds:.0f}{' failed=' + o.failed if o.failed else ''}"
        )
    hits = sum(1 for o in outcomes if o.as_expected)
    print(f"  {hits} of {len(outcomes)} as expected (the aim is 3 or more)")
    if not outcomes:
        pytest.fail("no case ran")
