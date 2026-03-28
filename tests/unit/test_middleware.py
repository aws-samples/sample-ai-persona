"""CSRFMiddleware の単体テスト"""
import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from web.middleware import CSRFMiddleware


@pytest.fixture
def app():
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)

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
        assert resp.json()["detail"] == "CSRF validation failed"
