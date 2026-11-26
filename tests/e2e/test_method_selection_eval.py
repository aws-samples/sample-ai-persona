"""Evaluation of how the analysis picks its method with the real models.

Not a pass/fail test: four cases run through the running app against the dev
stack (as ``test_advanced_generation.py``), with the AI detailed analysis on
and the plan confirmation checked, and what was chosen is printed. The aim is
three of the four as expected; the result is recorded
by hand.

    uv run pytest tests/e2e/test_method_selection_eval.py -m e2e -s

Cases (made-up data in ``tests/e2e/data``):

1. join: customers and their purchases share ``customer_id`` -> a join
2. compare_groups: customers, "compare the plans" -> a comparison of groups
3. rfm: customers and purchases, "good and drifting customers" -> RFM
4. no common column: customers and the survey sample -> no join

A case is as expected when any of these holds (not a word in the
code, which a variable name could hold): the plan's steps hold the expected
kind (the plan card), the expected playbook was read (the record), or a
successful AI-written block calls the expected function (read with ``ast``).
For the comparison, a ``compare`` step with the fixed tools also counts.
"""

from __future__ import annotations

import ast
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from playwright.sync_api import BrowserContext, Page, expect

from tests.e2e.conftest import DATA_DIR
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

SURVEY_CSV = DATA_DIR / "advanced_sample.csv"
NO_JOIN = "no-join"


@dataclass(frozen=True)
class Case:
    name: str
    files: tuple[Path, ...]
    purpose: str
    #: Step kinds that count (the plan card's ``data-kind``)
    kinds: tuple[str, ...] = ()
    #: The playbook that counts when read
    playbook: str = ""
    #: The library function that counts when a successful block calls it
    function: str = ""
    #: ``no-join``: as expected when nothing was joined
    expected: str = ""


CASES = (
    Case(
        "join",
        (CUSTOMERS_CSV, PURCHASES_CSV),
        "購入の多いお客さまの層を知りたい",
        kinds=("join",),
        playbook="join",
        function="join_tables",
    ),
    Case(
        "compare_groups",
        (CUSTOMERS_CSV,),
        "ゴールドとベーシックの会員で年齢の違いを比べたい",
        # a comparison with the fixed tools counts as well
        kinds=("compare_groups", "compare"),
        playbook="compare_groups",
        function="compare_groups",
    ),
    Case(
        "rfm",
        (CUSTOMERS_CSV, PURCHASES_CSV),
        "購入の新しさ・頻度・金額で、優良なお客さまと離れそうなお客さまを分けたい",
        kinds=("rfm",),
        playbook="rfm",
        function="rfm",
    ),
    Case("no_common_column", (CUSTOMERS_CSV, SURVEY_CSV), "", expected=NO_JOIN),
)


def calls_function(code: str, name: str) -> bool:
    """Whether ``code`` calls ``name`` (``f(..)`` or ``x.f(..)``), read with ``ast``.

    Comments, strings and variable names never count; code that does not
    parse counts as no call.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == name:
            return True
        if isinstance(func, ast.Attribute) and func.attr == name:
            return True
    return False


@dataclass
class Outcome:
    case: Case
    steps: list[str] = field(default_factory=list)
    playbooks: list[str] = field(default_factory=list)
    #: Successful AI-written blocks only (failed runs are never recorded)
    ai_code: list[str] = field(default_factory=list)
    joined: bool = False
    seconds: float = 0.0
    failed: str = ""

    @property
    def by_step(self) -> bool:
        return any(kind in self.case.kinds for kind in self.steps)

    @property
    def by_playbook(self) -> bool:
        return bool(self.case.playbook) and self.case.playbook in self.playbooks

    @property
    def by_code(self) -> bool:
        return bool(self.case.function) and any(
            calls_function(code, self.case.function) for code in self.ai_code
        )

    @property
    def as_expected(self) -> bool:
        if self.failed:
            return False
        if self.case.expected == NO_JOIN:
            return not self.joined
        return self.by_step or self.by_playbook or self.by_code


def _run_case(page: Page, case: Case, run_tag: str) -> Outcome:
    outcome = Outcome(case)
    _open_advanced_page(page)
    for path in case.files:
        _upload_named(page, path)
    page.get_by_test_id("advanced-purpose").fill(f"{run_tag} {case.purpose}".strip())
    page.get_by_test_id("advanced-persona-count").select_option("2")
    page.get_by_test_id("advanced-confirm-plan").check()
    started = time.monotonic()
    page.get_by_test_id("advanced-generate-button").click()
    card = page.get_by_test_id("advanced-plan-card")
    page.locator(
        '[data-testid="advanced-plan-approve"], '
        '[data-testid="advanced-failure"]:not([hidden])'
    ).first.wait_for(timeout=PLAN_TIMEOUT_MS)
    failure = page.get_by_test_id("advanced-failure")
    if failure.is_visible():
        outcome.failed = failure.inner_text()[:200]
        return outcome
    for step in card.get_by_test_id("advanced-plan-step").all():
        outcome.steps.append(step.get_attribute("data-kind") or "")
    card.get_by_test_id("advanced-plan-approve").click()
    page.locator(
        '[data-testid="advanced-result"], [data-testid="advanced-failure"]:not([hidden])'
    ).first.wait_for(timeout=GENERATION_TIMEOUT_MS)
    outcome.seconds = time.monotonic() - started
    if failure.is_visible():
        outcome.failed = failure.inner_text()[:200]
        return outcome
    result = page.get_by_test_id("advanced-result")
    result.get_by_role("button", name="分析の詳細").click()
    record = page.get_by_test_id("advanced-record")
    expect(record).to_be_visible()
    for name in record.get_by_test_id("advanced-record-playbook").all():
        outcome.playbooks.append(name.get_attribute("data-name") or "")
    for block in record.get_by_test_id("advanced-record-code").all():
        summary = block.locator("summary")
        if summary.get_attribute("data-kind") != "ai_written":
            continue
        summary.click()
        outcome.ai_code.append(block.locator("pre code").inner_text())
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

    print("\n[U6 eval] method selection (steps / playbooks / AI code / joined)")
    for o in outcomes:
        print(
            f"  {o.case.name}: as_expected={o.as_expected} "
            f"steps={','.join(o.steps) or '-'} by_step={o.by_step} "
            f"playbooks={','.join(o.playbooks) or '-'} by_playbook={o.by_playbook} "
            f"ai_written={len(o.ai_code)} by_code={o.by_code} joined={o.joined} "
            f"seconds={o.seconds:.0f}{' failed=' + o.failed if o.failed else ''}"
        )
    hits = sum(1 for o in outcomes if o.as_expected)
    print(f"  {hits} of {len(outcomes)} as expected (the aim is 3 or more)")
    if not outcomes:
        pytest.fail("no case ran")
