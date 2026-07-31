#!/bin/bash
set -euo pipefail
cd /Users/example/.hermes/experiments/kuies-short-url
set -a
source ./.env
set +a
export SHORT_PREVIEW_IMAGE_DIR="/Volumes/external/kuies-short-url/preview-images"
exec /Users/example/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/bin/python3.11 app.py \
  --host "$SHORT_HOST" \
  --port "$SHORT_PORT" \
  --db "$SHORT_DB" \
  --base-url "$SHORT_BASE_URL" \
  --admin-token "$SHORT_ADMIN_KEY"
