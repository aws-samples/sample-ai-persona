"""static_url: versioned static asset URLs so a deploy is not hidden by the CDN cache."""

import hashlib
import re
from pathlib import Path

import pytest

from web import static_assets
from web.static_assets import static_url

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "web" / "static"


@pytest.fixture(autouse=True)
def _fresh_cache():
    static_assets.static_url.cache_clear()
    yield
    static_assets.static_url.cache_clear()


def test_url_carries_the_hash_of_the_file_content() -> None:
    digest = hashlib.sha256((STATIC / "js" / "app.js").read_bytes()).hexdigest()[:12]
    assert static_url("js/app.js") == f"/static/js/app.js?v={digest}"


def test_changing_the_content_changes_the_url(tmp_path, monkeypatch) -> None:
    (tmp_path / "a.js").write_text("one")
    monkeypatch.setattr(static_assets, "STATIC_DIR", tmp_path)
    first = static_url("a.js")
    static_assets.static_url.cache_clear()
    (tmp_path / "a.js").write_text("two")
    assert static_url("a.js") != first


def test_missing_file_still_returns_a_plain_url(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(static_assets, "STATIC_DIR", tmp_path)
    assert static_url("missing.js") == "/static/missing.js"


def test_paths_outside_the_static_directory_are_rejected() -> None:
    with pytest.raises(ValueError):
        static_url("../main.py")


@pytest.mark.parametrize("template", ["base.html", "auth/layout.html"])
def test_pages_load_own_assets_through_static_url(template: str) -> None:
    """Own assets must be versioned; a bare /static/... URL stays cached for a day."""
    text = (ROOT / "web" / "templates" / template).read_text()
    bare = re.findall(r'(?:src|href)="/static/(?!js/vendor/dicebear)[^"?]+"', text)
    assert bare == []
