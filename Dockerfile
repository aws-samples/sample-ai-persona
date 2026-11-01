# 参考：https://docs.astral.sh/uv/guides/integration/docker/#available-images
FROM python:3.13-slim-bookworm

# uv はビルド用にのみ使用する（uvx は実行時MCP起動に使っていたが廃止したため入れない）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/

# アプリ本体をコピー
ADD . /app

# 作業ディレクトリ
WORKDIR /app

# 依存パッケージをインストールし、DuckDB の httpfs 拡張をビルド時に組み込む
# （実行時の INSTALL httpfs によるインターネットダウンロードを排除する）
RUN uv sync --frozen \
    && /app/.venv/bin/python -c "import duckdb; c=duckdb.connect(); c.execute('INSTALL httpfs'); c.execute('LOAD httpfs'); c.close()"

# 公開ポート
EXPOSE 80

# FastAPI + uvicornを起動（uv run を介さず venv 直起動）
# --no-access-log: uvicorn のアクセスログは URL のクエリ（/auth/callback の code・state）を
#   そのまま出すため止め、アプリの http.access（クエリを除いたパス）に置き換える（NFR3.3）
# --timeout-keep-alive 310: 内部 ALB の無操作タイムアウト（300 秒）より長くし、ALB より先に
#   アプリが接続を閉じて 502 になるのを防ぐ
# --timeout-graceful-shutdown 60: SIGTERM のあと接続の終わりを待つのは 60 秒まで。そのあとの
#   lifespan の終了でアンケートの進行役の回を最長 30 秒待つので、合わせて ECS の stopTimeout
#   （120 秒）に収まる（NFR4.11）
CMD ["/app/.venv/bin/uvicorn", "web.main:app", "--host", "0.0.0.0", "--port", "80", "--no-access-log", "--timeout-keep-alive", "310", "--timeout-graceful-shutdown", "60"]
