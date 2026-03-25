# 参考：https://docs.astral.sh/uv/guides/integration/docker/#available-images
FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# アプリ本体をコピー
ADD . /app

# 作業ディレクトリ
WORKDIR /app

# 依存パッケージをインストール
RUN uv sync --frozen

# 公開ポート
EXPOSE 80

# FastAPI + uvicornを起動
CMD ["uv", "run", "uvicorn", "web.main:app", "--host", "0.0.0.0", "--port", "80"]
