#!/usr/bin/env python3
"""Refresh URLCheck's ClearURLs catalog with conservative social tracking rules."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import build_urlcheck_social_catalog

UPSTREAM_URL = "https://rules2.clearurls.xyz/data.minify.json"
OUTPUT_DIR = PROJECT_ROOT / "static" / "urlcheck"
CATALOG_PATH = OUTPUT_DIR / "social-rules.json"
HASH_PATH = OUTPUT_DIR / "social-rules.sha256"


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def main() -> int:
    request = Request(UPSTREAM_URL, headers={"User-Agent": "kuies-urlcheck-catalog/1.0"})
    with urlopen(request, timeout=30) as response:
        upstream = json.loads(response.read().decode("utf-8"))

    providers = upstream.get("providers")
    if not isinstance(providers, dict) or len(providers) < 100:
        raise ValueError("Upstream ClearURLs catalog is incomplete")

    catalog = build_urlcheck_social_catalog(upstream)
    raw = json.dumps(catalog, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest().encode("ascii") + b"\n"

    atomic_write(CATALOG_PATH, raw)
    atomic_write(HASH_PATH, digest)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"URLCheck 規則更新失敗：{error}", file=sys.stderr)
        raise SystemExit(1)
