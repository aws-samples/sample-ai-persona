# 参考：https://docs.astral.sh/uv/guides/integration/docker/#available-images
FROM python:3.13-slim-bookworm

# uv はビルド用にのみ使用する（uvx は実行時MCP起動に使っていたが廃止したため入れない）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/

# 実行用の非ルートユーザー（UID/GID 10001。数値で指定し、オーケストレーター側の runAsNonRoot 検査にも通す）
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /home/app --create-home --shell /usr/sbin/nologin app

# アプリ本体をコピー（所有者は root のまま。実行ユーザーからは読み取り専用）
ADD . /app

# 作業ディレクトリ
WORKDIR /app

# 依存パッケージをインストールする。実行時に書き込むのはローカル保存用の uploads/ だけなので、
# そこだけ実行ユーザーの所有にする（S3 を使う構成では空のまま）
RUN uv sync --frozen \
    && install -d -o app -g app /app/uploads

# ここから実行ユーザーに切り替える
USER 10001:10001
ENV HOME=/home/app

# DuckDB の httpfs 拡張をビルド時に組み込む（実行時の INSTALL httpfs によるインターネット
# ダウンロードを排除する）。拡張は $HOME/.duckdb に入るため、実行ユーザーで入れる
RUN /app/.venv/bin/python -c "import duckdb; c=duckdb.connect(); c.execute('INSTALL httpfs'); c.execute('LOAD httpfs'); c.close()"

# 公開ポート（非ルートは 1024 未満を bind できないため 8080。ALB のリスナーは 80 のまま）
EXPOSE 8080

# FastAPI + uvicornを起動（uv run を介さず venv 直起動）
# --no-access-log: uvicorn のアクセスログは URL のクエリ（/auth/callback の code・state）を
#   そのまま出すため止め、アプリの http.access（クエリを除いたパス）に置き換える（NFR3.3）
# --timeout-keep-alive 310: 内部 ALB の無操作タイムアウト（300 秒）より長くし、ALB より先に
#   アプリが接続を閉じて 502 になるのを防ぐ
# --timeout-graceful-shutdown 60: SIGTERM のあと接続の終わりを待つのは 60 秒まで。そのあとの
#   lifespan の終了でアンケートの進行役の回を最長 30 秒待つので、合わせて ECS の stopTimeout
#   （120 秒）に収まる（NFR4.11）
CMD ["/app/.venv/bin/uvicorn", "web.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--timeout-keep-alive", "310", "--timeout-graceful-shutdown", "60"]
