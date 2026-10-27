"""U4 の応答への変換 asset_response（BR4.1、BR4.3、BR4.5）。"""

import asyncio
from unittest.mock import Mock
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.models.asset_delivery import AssetStream, DeliveryResult
from web.routers._asset_response import asset_response, content_disposition

pytestmark = pytest.mark.api


def _result(chunks, close: Mock, content_type="application/pdf", name="資料.pdf"):
    asset = AssetStream(chunks=chunks, content_length=8, close=close)
    return DeliveryResult.stream(asset, content_type, name)


class TestRedirect:
    def test_redirect_is_302_without_cache_header_by_default(self):
        response = asset_response(DeliveryResult.redirect("https://signed/x"))
        assert response.status_code == 302
        assert response.headers["location"] == "https://signed/x"
        assert "cache-control" not in response.headers

    def test_redirect_keeps_a_given_cache_header(self):
        response = asset_response(
            DeliveryResult.redirect("https://signed/x"),
            cache_control="private, max-age=300",
        )
        assert response.headers["cache-control"] == "private, max-age=300"


class TestStreamHeaders:
    def test_headers_of_a_stream(self):
        response = asset_response(_result(iter([b"%PDF"]), Mock()))
        assert response.status_code == 200
        assert response.media_type == "application/pdf"
        assert response.headers["content-length"] == "8"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "private, no-store"
        disposition = response.headers["content-disposition"]
        assert disposition.startswith("inline;")
        assert f"filename*=UTF-8''{quote('資料.pdf')}" in disposition

    def test_disposition_ascii_fallback(self):
        value = content_disposition("attachment", "結果.csv")
        assert value.startswith('attachment; filename="')
        assert '"__.csv"' in value
        assert value.endswith(f"filename*=UTF-8''{quote('結果.csv')}")

    @pytest.mark.parametrize(
        "name", ["a\r\nX-Evil: 1.csv", "a\x00b.csv", "a\tb\x7f.csv"]
    )
    def test_disposition_drops_control_characters(self, name):
        value = content_disposition("attachment", name)
        assert not any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)
        assert "%0D" not in value and "%0A" not in value and "%00" not in value


class TestBodyIsAlwaysClosed:
    """BR4.5: 読み終わり・途中の失敗・切断のどれでも close が呼ばれる。"""

    def _app(self, result: DeliveryResult) -> TestClient:
        app = FastAPI()

        @app.get("/f")
        def _f():  # pragma: no cover - exercised through the client
            return asset_response(result)

        return TestClient(app, raise_server_exceptions=False)

    def test_read_to_the_end(self):
        close = Mock()
        client = self._app(_result(iter([b"abcd", b"efgh"]), close))

        response = client.get("/f")

        assert response.content == b"abcdefgh"
        close.assert_called()

    def test_failure_while_streaming(self):
        close = Mock()

        def broken():
            yield b"abcd"
            raise ConnectionResetError("s3 broke")

        client = self._app(_result(broken(), close))
        try:
            response = client.get("/f")
        except ConnectionResetError:
            pass  # the transport may surface the broken stream
        else:
            # The status is already sent; the body stops short of Content-Length
            assert response.status_code == 200
            assert len(response.content) < 8

        close.assert_called()

    @pytest.mark.parametrize(
        ("fail_on", "spec_version"),
        [
            ("http.response.start", "2.4"),  # 最初のデータを送る前
            ("http.response.body", "2.4"),  # 途中まで送ったあと
            ("http.response.start", "2.0"),
            ("http.response.body", "2.0"),
        ],
    )
    def test_send_failure_closes_the_body(self, fail_on, spec_version):
        """送信そのものが失敗しても（後始末のタスクが走らなくても）閉じる。"""
        close = Mock()
        response = asset_response(_result(iter([b"a"] * 100), close))
        scope = {"type": "http", "asgi": {"spec_version": spec_version}}
        sent: list[str] = []

        async def receive():
            await asyncio.sleep(3600)  # the browser never says it left
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message["type"])
            body_count = sent.count("http.response.body")
            if message["type"] == fail_on and (
                fail_on != "http.response.body" or body_count == 3
            ):
                raise OSError("connection reset by peer")

        async def run() -> None:
            with pytest.raises(Exception):
                await response(scope, receive, send)
            # Checked while the loop still runs: a real server keeps its loop,
            # so closing at loop shutdown or by GC does not count.
            close.assert_called()

        asyncio.run(run())

    def test_client_disconnect_after_a_few_chunks(self):
        """The server reports the browser left (ASGI < 2.4) while chunks flow."""
        close = Mock()

        def endless():
            while True:
                yield b"a"

        response = asset_response(_result(endless(), close))
        scope = {"type": "http", "asgi": {"spec_version": "2.0"}}
        bodies = 0
        left = asyncio.Event()

        async def receive():
            await left.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            nonlocal bodies
            if message["type"] == "http.response.body":
                bodies += 1
                if bodies == 2:
                    left.set()
            await asyncio.sleep(0)

        async def run() -> None:
            await response(scope, receive, send)
            close.assert_called()  # while the loop still runs

        asyncio.run(run())
