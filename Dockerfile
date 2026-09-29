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
CMD ["/app/.venv/bin/uvicorn", "web.main:app", "--host", "0.0.0.0", "--port", "80"]
