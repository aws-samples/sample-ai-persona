"""Pure helpers of the document part of the advanced generation.

The Manager orders the work; these functions only shape values: the progress
wording, the joined full text, the renumbered evidence, the assignment to
segments and the trimming before the result is written.
"""

from __future__ import annotations

import json

import pytest

from src.managers.advanced_generation import documents
from src.managers.advanced_generation.prompts import (
    DocumentEvidenceOutput,
    EvidenceAssignmentItem,
    EvidenceExtraction,
)
from src.models.analysis import DocumentReport, Evidence, EvidenceKind

pytestmark = pytest.mark.unit

INFO = {"page_from": 3, "page_to": 5, "heading_path": ["第2章", "不満"]}


class TestWording:
    @pytest.mark.parametrize(
        ("chars", "man"), [(0, 0), (1, 1), (10_000, 1), (10_001, 2), (850_000, 85)]
    )
    def test_ten_thousands_round_up(self, chars: int, man: int) -> None:
        assert documents.man_chars(chars) == man

    def test_a_pdf_report_says_pages_and_the_left_out_pages(self) -> None:
        report = DocumentReport(
            source_id="d",
            chars=42_000,
            chunks=3,
            headings=0,
            no_text_pages=(2, 7),
            pages=12,
        )

        message = documents.prepared_message(1, report)

        assert message.startswith("1 件目の文書: 12 ページ・約 5 万字・分割 3 個")
        assert "2、7 ページは文字がないため除外しました" in message

    def test_many_left_out_pages_show_the_first_and_the_count(self) -> None:
        report = DocumentReport(
            source_id="d",
            chars=10,
            chunks=1,
            headings=0,
            no_text_pages=tuple(range(1, 30)),
            pages=40,
        )

        message = documents.prepared_message(2, report)

        assert "1、2、3、4、5 など 29 ページ" in message

    def test_a_text_report_has_no_pages(self) -> None:
        report = DocumentReport(source_id="d", chars=12_000, chunks=1, headings=2)

        assert "ページ" not in documents.prepared_message(1, report)


class TestJoinChunks:
    def test_the_overlap_is_taken_off_every_chunk_but_the_first(self) -> None:
        text = "abcdefghijklmnopqrstuvwxyz"
        chunks = [text[0:10], text[7:17], text[14:26]]

        assert documents.join_chunks(chunks, overlap=3) == text

    def test_one_chunk_is_itself(self) -> None:
        assert documents.join_chunks(["only"], overlap=3) == "only"


class TestExtractionEvidence:
    def _output(self, *items: dict) -> EvidenceExtraction:
        return EvidenceExtraction(items=list(items))

    def _item(self, **changes) -> dict:
        item = {
            "kind": "pain",
            "claim": "待ち時間",
            "quote": "受付で三十分も待たされたのがつらかった",
            "speaker_attributes": [{"name": "年代", "value": "30代"}],
        }
        item.update(changes)
        return item

    def test_ids_source_and_location_come_from_the_manager(self) -> None:
        evidence = documents.evidence_from_extraction(
            self._output(self._item(page=4), self._item(kind="goal")),
            source_index=2,
            source_id="doc-9",
            chunk_index=5,
            info=INFO,
            max_items=20,
            max_quote_chars=200,
        )

        assert [e.id for e in evidence] == ["ev-d2-5-0", "ev-d2-5-1"]
        assert {e.source_id for e in evidence} == {"doc-9"}
        assert evidence[0].location == "p.4 / 第2章 > 不満"
        assert evidence[1].location == "p.3-5 / 第2章 > 不満"
        assert evidence[0].speaker_attributes == {"年代": "30代"}

    def test_a_page_outside_the_chunk_is_not_trusted(self) -> None:
        (item,) = documents.evidence_from_extraction(
            self._output(self._item(page=99)),
            source_index=0,
            source_id="d",
            chunk_index=0,
            info=INFO,
            max_items=20,
            max_quote_chars=200,
        )

        assert item.location.startswith("p.3-5")

    def test_items_and_quotes_are_cut(self) -> None:
        evidence = documents.evidence_from_extraction(
            self._output(*[self._item() for _ in range(5)], self._item(quote="  ")),
            source_index=0,
            source_id="d",
            chunk_index=0,
            info={},
            max_items=3,
            max_quote_chars=6,
        )

        assert len(evidence) == 3
        assert all(len(e.quote or "") == 6 for e in evidence)

    def test_no_output_is_no_evidence(self) -> None:
        assert (
            documents.evidence_from_extraction(
                None,
                source_index=0,
                source_id="d",
                chunk_index=0,
                info={},
                max_items=3,
                max_quote_chars=6,
            )
            == []
        )


class TestSynthesisEvidence:
    def test_model_ids_are_replaced_and_unknown_documents_dropped(self) -> None:
        items = [
            DocumentEvidenceOutput(
                id="m1",
                document_index=0,
                kind="value",
                claim="c",
                quote="家族との時間を大切にしたい",
                segment_id="seg-1",
                assignment_reason="r",
            ),
            DocumentEvidenceOutput(
                id="m2",
                document_index=1,
                kind="pain",
                claim="c",
                quote="受付で三十分も待たされた",
                segment_id="seg-x",
            ),
            DocumentEvidenceOutput(
                id="m3",
                document_index=7,
                kind="goal",
                claim="c",
                quote="存在しない文書",
            ),
        ]

        evidence, ids = documents.evidence_from_synthesis(
            items,
            documents=[(0, "doc-a"), (3, "doc-b")],
            segment_ids={"seg-1"},
            max_quote_chars=200,
        )

        assert [e.id for e in evidence] == ["ev-d0-0-0", "ev-d3-0-1"]
        assert ids == {"m1": "ev-d0-0-0", "m2": "ev-d3-0-1"}
        assert evidence[0].segment_id == "seg-1"
        assert evidence[0].assignment_reason == "r"
        assert evidence[1].segment_id is None and evidence[1].assignment_reason is None
        assert evidence[1].source_id == "doc-b"


class TestAssignments:
    def test_known_ids_get_their_segment_and_reason(self) -> None:
        ev = [
            Evidence.for_document(
                id=i,
                source_id="d",
                location="p.1",
                kind=EvidenceKind.VALUE,
                claim="c",
                quote="引用の文章です",
            )
            for i in ("a", "b", "c")
        ]
        assignments = [
            EvidenceAssignmentItem(evidence_id="a", segment_id="seg-1", reason="合う"),
            EvidenceAssignmentItem(evidence_id="b", segment_id="seg-x", reason="?"),
            EvidenceAssignmentItem(evidence_id="z", segment_id="seg-1", reason="?"),
        ]

        out = documents.apply_assignments(ev, assignments, segment_ids={"seg-1"})

        assert [(e.id, e.segment_id, e.assignment_reason) for e in out] == [
            ("a", "seg-1", "合う"),
            ("b", None, None),
            ("c", None, None),
        ]


class TestTrim:
    def _ev(self, eid: str, size: int = 100, quote: bool = True) -> Evidence:
        if not quote:
            return Evidence(
                id=eid,
                source_id="t",
                location="tc",
                kind=EvidenceKind.ATTRIBUTE,
                claim="x" * size,
            )
        return Evidence.for_document(
            id=eid,
            source_id="d",
            location="p.1",
            kind=EvidenceKind.VALUE,
            claim="x" * size,
            quote="引用の文章です",
        )

    @staticmethod
    def _size(item: Evidence) -> int:
        return len(json.dumps(item.to_dict()))

    def test_everything_that_fits_is_kept(self) -> None:
        evidence = [self._ev("a"), self._ev("b")]

        assert (
            documents.trim_evidence(
                evidence, cited=set(), budget=10_000, size_of=self._size
            )
            == evidence
        )

    def test_uncited_document_evidence_goes_first_from_the_end(self) -> None:
        evidence = [
            self._ev("table", quote=False),
            self._ev("cited"),
            self._ev("free-1"),
            self._ev("free-2"),
        ]
        budget = sum(self._size(e) for e in evidence[:3])

        kept = documents.trim_evidence(
            evidence, cited={"cited"}, budget=budget, size_of=self._size
        )

        assert [e.id for e in kept] == ["table", "cited", "free-1"]

    def test_cited_and_table_evidence_are_never_dropped(self) -> None:
        evidence = [self._ev("table", quote=False), self._ev("cited")]

        kept = documents.trim_evidence(
            evidence, cited={"cited"}, budget=1, size_of=self._size
        )

        assert kept == evidence
