"""CSRFMiddleware の単体テスト（BR1.19、NFR4.2、Functional Design レビュー R-12）"""

import io
import json

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from src.models.errors import ErrorCode
from web.middleware import CSRFMiddleware

APP_BASE_URL = "https://app.example.com"


@pytest.fixture
def log_stream() -> io.StringIO:
    return io.StringIO()


@pytest.fixture
def app(log_stream):
    app = FastAPI()
    app.add_middleware(CSRFMiddleware, app_base_url=APP_BASE_URL, log_stream=log_stream)

    @app.post("/api/thing")
    async def post_api():
        return PlainTextResponse("ok")

    @app.post("/signout")
    async def signout():
        return PlainTextResponse("signed out")

    @app.get("/test")
    async def get_test():
        return PlainTextResponse("ok")

    @app.post("/test")
    async def post_test():
        return PlainTextResponse("ok")

    @app.put("/test")
    async def put_test():
        return PlainTextResponse("ok")

    @app.delete("/test")
    async def delete_test():
        return PlainTextResponse("ok")

    @app.patch("/test")
    async def patch_test():
        return PlainTextResponse("ok")

    @app.get("/health")
    async def health():
        return PlainTextResponse("healthy")

    @app.post("/health")
    async def post_health():
        return PlainTextResponse("healthy")

    return app


@pytest.fixture
def client(app):
    return TestClient(app)


class TestCSRFMiddleware:
    def test_get_always_passes(self, client):
        resp = client.get("/test")
        assert resp.status_code == 200

    def test_post_without_header_blocked(self, client):
        resp = client.post("/test", content="data")
        assert resp.status_code == 403

    def test_post_with_hx_request_passes(self, client):
        resp = client.post("/test", headers={"HX-Request": "true"})
        assert resp.status_code == 200

    def test_post_with_x_requested_with_passes(self, client):
        resp = client.post("/test", headers={"X-Requested-With": "XMLHttpRequest"})
        assert resp.status_code == 200

    def test_put_without_header_blocked(self, client):
        resp = client.put("/test", content="data")
        assert resp.status_code == 403

    def test_delete_without_header_blocked(self, client):
        resp = client.delete("/test")
        assert resp.status_code == 403

    def test_patch_without_header_blocked(self, client):
        resp = client.patch("/test", content="data")
        assert resp.status_code == 403

    def test_exempt_path_post_passes(self, client):
        resp = client.post("/health", content="data")
        assert resp.status_code == 200

    def test_csrf_error_response_body(self, client):
        resp = client.post("/test", content="data")
        body = resp.json()
        assert body["error_code"] == ErrorCode.CSRF_FAILED
        assert body["detail"]


class TestCSRFv1:
    """v1.0.0 の変更: /api/ の除外をやめ、/signout は Origin で確かめる。"""

    def test_api_post_without_custom_header_is_blocked(self, client):
        resp = client.post("/api/thing", json={})
        assert resp.status_code == 403
        assert resp.json()["error_code"] == ErrorCode.CSRF_FAILED

    def test_api_post_with_custom_header_passes(self, client):
        resp = client.post("/api/thing", headers={"X-Requested-With": "XMLHttpRequest"})
        assert resp.status_code == 200

    def test_signout_form_from_the_app_itself_passes(self, client):
        resp = client.post("/signout", headers={"Origin": APP_BASE_URL})
        assert resp.status_code == 200

    @pytest.mark.parametrize(
        "origin",
        [
            "https://evil.example.com",
            "http://app.example.com",  # スキームが違う
            "https://app.example.com:8443",  # ポートが違う
            "https://app.example.com.evil.test",
            "null",
        ],
    )
    def test_signout_from_another_origin_is_blocked(self, client, origin):
        resp = client.post("/signout", headers={"Origin": origin})
        assert resp.status_code == 403

    def test_signout_without_origin_is_blocked(self, client):
        assert client.post("/signout").status_code == 403

    def test_rejection_is_logged(self, client, log_stream):
        client.post("/api/thing", json={})
        events = [json.loads(line) for line in log_stream.getvalue().splitlines()]
        assert events[-1]["event"] == "auth.csrf_rejected"
        assert events[-1]["path"] == "/api/thing"
