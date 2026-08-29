# MCP server image (Streamable HTTP). Built and run by Render from `render.yaml`.
# The ~730 MB SQLite mirror is NOT baked in -- it is downloaded at boot from a
# GitHub Release (see src/knesset_utils/server/mirror.py) onto the /data volume.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    MCP_DB_PATH=/data/knesset_mirror.sqlite

WORKDIR /app

# Install first (better layer caching): only pyproject + source affect the build.
COPY pyproject.toml ./
COPY src ./src
RUN pip install ".[server]"

# Non-root. /data is where the mirror volume mounts.
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /data \
 && chown -R appuser:appuser /data /app
USER appuser

EXPOSE 8000

# Render uses `healthCheckPath` and ignores this; kept for plain `docker run`.
# stdlib only -- no curl in the slim image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
  CMD python -c "import os,sys,urllib.request; \
port=os.environ.get('PORT') or os.environ.get('MCP_PORT','8000'); \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % port, timeout=4).status == 200 else 1)"

CMD ["python", "-m", "knesset_utils.server.mcp_server"]
