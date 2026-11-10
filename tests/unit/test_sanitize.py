"""render_markdown (sanitize.py) の単体テスト"""

import time

import pytest

from web.sanitize import render_markdown


class TestRenderMarkdown:
    def test_basic_markdown(self):
        result = render_markdown("**太字**")
        assert "<strong>太字</strong>" in result

    def test_heading(self):
        result = render_markdown("# 見出し")
        assert "<h1>" in result

    def test_list(self):
        result = render_markdown("- item1\n- item2")
        assert "<li>" in result

    def test_code_block(self):
        result = render_markdown("```\ncode\n```")
        assert "<code>" in result

    def test_link_preserved(self):
        result = render_markdown("[リンク](https://example.com)")
        assert 'href="https://example.com"' in result

    def test_table(self):
        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        result = render_markdown(md)
        assert "<table>" in result

    # XSS prevention tests
    def test_script_tag_stripped(self):
        result = render_markdown("<script>alert('xss')</script>")
        assert "<script>" not in result
        assert "alert" not in result or "<script>" not in result

    def test_onclick_stripped(self):
        result = render_markdown('<div onclick="alert(1)">test</div>')
        assert "onclick" not in result

    def test_javascript_href_stripped(self):
        result = render_markdown('<a href="javascript:alert(1)">click</a>')
        assert "javascript:" not in result

    def test_img_onerror_stripped(self):
        result = render_markdown('<img src="x" onerror="alert(1)">')
        assert "onerror" not in result

    # Allowed tags pass through
    def test_allowed_tags_preserved(self):
        result = render_markdown("*italic* **bold** ~~strike~~")
        assert "<em>" in result
        assert "<strong>" in result

    # URL auto-linkify tests
    def test_plain_url_linkified(self):
        result = render_markdown("ダウンロード: https://example.com/file.csv")
        assert 'href="https://example.com/file.csv"' in result
        assert 'target="_blank"' in result

    def test_existing_markdown_link_not_double_linked(self):
        result = render_markdown("[リンク](https://example.com/page)")
        assert result.count("https://example.com/page") == 1

    def test_url_in_code_block_not_linkified(self):
        result = render_markdown("`https://example.com/code`")
        assert "<a" not in result or 'href="https://example.com/code"' not in result

    def test_presigned_url_linkified(self):
        url = "https://bucket.s3.amazonaws.com/key?X-Amz-Signature=abc123"
        result = render_markdown(f"CSV: {url}")
        assert f'href="{url}"' in result

    def test_single_newline_becomes_line_break(self):
        result = render_markdown("1行目\n2行目")
        assert "<br" in result

    def test_fenced_code_keeps_language_class(self):
        result = render_markdown("```python\nprint(1)\n```")
        assert 'class="language-python"' in result

    def test_allowed_raw_html_passes_through(self):
        result = render_markdown("<u>下線</u>")
        assert "<u>下線</u>" in result

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("これは**「重要」**です", "<strong>「重要」</strong>"),
            ("結果は**50%**でした", "<strong>50%</strong>"),
            ("*「強調」*します", "<em>「強調」</em>"),
        ],
    )
    def test_emphasis_next_to_japanese(self, text, expected):
        assert expected in render_markdown(text)

    def test_emphasis_still_needs_a_closing_marker(self):
        assert "<strong>" not in render_markdown("これは**閉じていない")


class TestRenderMarkdownCost:
    """Rendering time must stay roughly linear in the input (CWE-1333)."""

    @pytest.mark.parametrize(
        "text",
        [
            "`" * 5000,
            "[" * 10000,
            "<code>" * 20000,
            "` " * 5000,
        ],
        ids=["backticks", "brackets", "unclosed-code", "spaced-backticks"],
    )
    def test_pathological_input_renders_quickly(self, text):
        started = time.monotonic()
        render_markdown(text)
        assert time.monotonic() - started < 1.0
