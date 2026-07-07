# blackbox-runner: the game server (bindings, rounds, scoring, admin/player
# web). TrackingBox is NOT part of this image -- it needs a camera, GPU, and
# Spout output, and runs on the venue machine. See docs/deployment.md.
FROM python:3.11-slim

WORKDIR /app

# Dependencies first so an app-code-only change doesn't invalidate this layer.
# Installed in place (not editable-into-site-packages) so server/app.py's
# `Path(__file__).parent.parent / "web"` keeps resolving to ./web -- the same
# assumption make dev relies on locally (see server/app.py, docs/runbook.md).
COPY pyproject.toml ./
COPY server ./server
RUN pip install --no-cache-dir -e .

COPY web ./web
COPY content ./content
COPY scripts ./scripts
COPY docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x docker-entrypoint.sh

ENV GAME_HOST=0.0.0.0 \
    GAME_PORT=8100 \
    GAME_DB_PATH=/app/data/blackbox-runner.db \
    GAME_CONTENT_PATH=content/show.yaml \
    GAME_AUDIO_DIR=content/audio

EXPOSE 8100
VOLUME ["/app/data"]

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8100/health', timeout=3)" || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
