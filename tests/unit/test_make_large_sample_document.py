"""scripts/make_large_sample_document.py: a made-up interview record.

The manual E2E of 800,000 characters uses it; here it is checked small. The
text is made up (a fixed vocabulary), each part has a heading and speakers
with attributes, and the file is written a little at a time.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import pytest

from scripts import make_large_sample_document as gen

pytestmark = pytest.mark.unit


class TestText:
    def test_has_the_characters_and_the_headings_asked_for(
        self, tmp_path: Path
    ) -> None:
        out = tmp_path / "interview.md"

        written = gen.write_document(str(out), chars=20_000, fmt="md", headings=5)

        text = out.read_text(encoding="utf-8")
        assert written.chars == len(text)
        assert 20_000 <= len(text) < 21_000
        assert len(re.findall(r"^# ", text, flags=re.MULTILINE)) == 5

    def test_lines_carry_a_speaker_with_attributes_and_labels(
        self, tmp_path: Path
    ) -> None:
        out = tmp_path / "interview.txt"

        gen.write_document(str(out), chars=5_000, fmt="txt", headings=2)

        text = out.read_text(encoding="utf-8")
        assert re.search(r"回答者[A-Z]（\d+代・(男性|女性)・\S+?）", text)
        for label in ("価値", "不満", "目標"):
            assert f"{label}:" in text

    def test_the_same_seed_writes_the_same_text(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a.txt", tmp_path / "b.txt"

        gen.write_document(str(a), chars=3_000, fmt="txt", headings=1, seed=7)
        gen.write_document(str(b), chars=3_000, fmt="txt", headings=1, seed=7)

        assert a.read_bytes() == b.read_bytes()


class TestDocx:
    def test_a_docx_has_heading_styles_and_the_characters(self, tmp_path: Path) -> None:
        out = tmp_path / "interview.docx"

        written = gen.write_document(str(out), chars=8_000, fmt="docx", headings=3)

        with zipfile.ZipFile(io.BytesIO(out.read_bytes())) as z:
            document = z.read("word/document.xml").decode("utf-8")
            styles = z.read("word/styles.xml").decode("utf-8")
        assert document.count('<w:pStyle w:val="Heading1"/>') == 3
        assert 'w:styleId="Heading1"' in styles
        assert 8_000 <= written.chars < 9_000

    def test_the_sandbox_preparation_reads_it_as_written(self, tmp_path: Path) -> None:
        import importlib.util

        library = (
            Path(__file__).resolve().parents[2] / "sandbox" / "persona_analysis.py"
        )
        spec = importlib.util.spec_from_file_location("pa_for_sample", library)
        assert spec is not None and spec.loader is not None
        lib = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lib)
        sources = tmp_path / "work" / "sources"
        sources.mkdir(parents=True)
        written = gen.write_document(
            str(sources / "doc1"), chars=40_000, fmt="docx", headings=4
        )

        result = lib.dispatch(
            "prepare_document",
            {
                "source_id": "doc1",
                "source_index": 0,
                "format": "docx",
                "chunk_chars": 15_000,
                "overlap_chars": 500,
            },
            str(tmp_path / "work"),
        )

        assert result["ok"] is True
        assert result["value"]["headings"] == 4
        assert abs(result["value"]["chars"] - written.chars) <= 2


class TestCommandLine:
    def test_main_writes_the_file(self, tmp_path: Path) -> None:
        out = tmp_path / "cli.md"

        code = gen.main(
            ["--chars", "2000", "--format", "md", "--out", str(out), "--headings", "2"]
        )

        assert code == 0
        assert out.stat().st_size > 0

    @pytest.mark.parametrize(
        "args",
        [["--chars", "0"], ["--headings", "0"], ["--format", "pdf"]],
    )
    def test_bad_arguments_stop(self, tmp_path: Path, args: list[str]) -> None:
        base = ["--chars", "100", "--format", "md", "--out", str(tmp_path / "x")]

        with pytest.raises(SystemExit):
            gen.main(base + args)
