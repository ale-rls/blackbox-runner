#!/bin/sh
# Default container command: push the authoring copy (content/show.yaml, or
# GAME_CONTENT_PATH) into the game DB, then serve. Content lives in git, not
# in the volume, so every deploy re-applies exactly what was committed --
# this is the same show.yaml -> import -> serve flow docs/runbook.md
# describes for local dev, just run automatically on container start instead
# of by hand. import_content.py validates before writing anything, so a bad
# show.yaml fails the container's startup instead of silently serving stale
# or broken content.
#
# Passing an explicit command (e.g. for `docker run <image> python
# scripts/replay.py ...`) skips the import and runs that command directly.
set -e

if [ "$#" -eq 0 ]; then
  echo "Importing ${GAME_CONTENT_PATH:-content/show.yaml} into ${GAME_DB_PATH:-data/blackbox-runner.db} ..."
  python scripts/import_content.py \
    --content "${GAME_CONTENT_PATH:-content/show.yaml}" \
    --db "${GAME_DB_PATH:-data/blackbox-runner.db}"
  exec python -m uvicorn server.app:app \
    --host "${GAME_HOST:-0.0.0.0}" \
    --port "${GAME_PORT:-8100}"
fi

exec "$@"
