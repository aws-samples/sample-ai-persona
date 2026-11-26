# Reference: https://docs.astral.sh/uv/guides/integration/docker/#available-images
FROM python:3.13-slim-bookworm

# uv is used at build time only; it is not needed at runtime.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/

# Non-root runtime user. UID/GID are numeric so orchestrator runAsNonRoot checks pass.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /home/app --create-home --shell /usr/sbin/nologin app

# Copy the app; owned by root, so read-only for the runtime user.
ADD . /app

# Working directory
WORKDIR /app

# Install dependencies. The only path written at runtime is uploads/ (local storage),
# so only that is owned by the runtime user (stays empty when S3 is used).
RUN uv sync --frozen \
    && install -d -o app -g app /app/uploads

# Switch to the runtime user from here on.
USER 10001:10001
ENV HOME=/home/app

# Bake the DuckDB httpfs extension in at build time so no internet download happens at
# runtime (INSTALL httpfs). It lands in $HOME/.duckdb, so install it as the runtime user.
RUN /app/.venv/bin/python -c "import duckdb; c=duckdb.connect(); c.execute('INSTALL httpfs'); c.execute('LOAD httpfs'); c.close()"

# Listen on 8080 because a non-root user cannot bind below 1024 (the ALB listener stays on 80).
EXPOSE 8080

# Start FastAPI with uvicorn directly from the venv (without uv run).
# --no-access-log: uvicorn access logs include the URL query (code/state on /auth/callback);
#   the app logs http.access (path without query) instead.
# --timeout-keep-alive 310: longer than the internal ALB idle timeout (300 s), so the app
#   never closes a connection first and causes a 502.
# --timeout-graceful-shutdown 60: after SIGTERM, wait up to 60 s for connections. The lifespan
#   shutdown then waits up to 30 s for the survey coordinator, so the total fits within the
#   ECS stopTimeout (120 s).
CMD ["/app/.venv/bin/uvicorn", "web.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--timeout-keep-alive", "310", "--timeout-graceful-shutdown", "60"]
