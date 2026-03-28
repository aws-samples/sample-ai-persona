"""render_markdown (sanitize.py) の単体テスト"""
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
