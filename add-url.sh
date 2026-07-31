#!/bin/bash
set -euo pipefail
if [ $# -lt 1 ] || [ $# -gt 2 ]; then
  echo "用法：$0 <長網址> [自訂短碼]" >&2
  exit 2
fi
cd /Users/example/.hermes/experiments/kuies-short-url
set -a
source ./.env
set +a
URL="$1"
CODE="${2:-}"
if [ -n "$CODE" ]; then
  BODY=$(python3 -c 'import json,sys; print(json.dumps({"url":sys.argv[1],"code":sys.argv[2]}, ensure_ascii=False))' "$URL" "$CODE")
else
  BODY=$(python3 -c 'import json,sys; print(json.dumps({"url":sys.argv[1]}, ensure_ascii=False))' "$URL")
fi
curl -sS -X POST "$SHORT_BASE_URL/api/urls" \
  -H "Authorization: Bearer $SHORT_ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d "$BODY"
echo
