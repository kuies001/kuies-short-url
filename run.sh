#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
set -a
source ./.env
set +a
# Preview image storage is deployment-specific; fall back to a local cache.
export SHORT_PREVIEW_IMAGE_DIR="${SHORT_PREVIEW_IMAGE_DIR:-$SCRIPT_DIR/data/preview-images}"
# The service host pins an interpreter that holds the external-disk permission;
# override with SHORT_PYTHON_BIN on other machines.
PYTHON_BIN="${SHORT_PYTHON_BIN:-$HOME/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/bin/python3.11}"
exec "$PYTHON_BIN" app.py \
  --host "$SHORT_HOST" \
  --port "$SHORT_PORT" \
  --db "$SHORT_DB" \
  --base-url "$SHORT_BASE_URL" \
  --admin-token "$SHORT_ADMIN_KEY"
