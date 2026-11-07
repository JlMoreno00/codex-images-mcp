# codex-images-mcp — self-hosted Streamable HTTP server.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CODEX_HOME=/data/codex \
    CODEX_IMAGES_OUTPUT_DIR=/data/out \
    CODEX_IMAGES_HTTP_HOST=0.0.0.0 \
    CODEX_IMAGES_HTTP_PORT=8324

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[http]" \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /data/codex /data/out \
    && chown -R app:app /data

USER app
WORKDIR /home/app
VOLUME ["/data"]
EXPOSE 8324

# Refuses to start on 0.0.0.0 unless CODEX_IMAGES_HTTP_TOKEN is set.
CMD ["codex-images-mcp-http"]
