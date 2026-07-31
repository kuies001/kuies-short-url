#!/usr/bin/env python3
"""Clean cached preview images for kuies-short-url.

Default behavior is quiet when successful. Prints only when files were removed or errors occur,
so it can be used with a no-agent cron job.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DB = PROJECT_DIR / "data" / "shorturls.sqlite3"
LOCAL_PREVIEW_DIR = PROJECT_DIR / "data" / "preview-images"
# External storage is opt-in. A mounted but unhealthy exFAT directory can block
# iterdir() indefinitely, so scheduled cleanup defaults to the local capped cache.
DEFAULT_DIR = Path(os.environ.get("SHORT_PREVIEW_IMAGE_DIR", str(LOCAL_PREVIEW_DIR)))
FALLBACK_DIR = LOCAL_PREVIEW_DIR
VALID_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def load_codes(db_path: Path) -> tuple[set[str], set[str]]:
    if not db_path.exists():
        return set(), set()
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT code, disabled_at FROM urls").fetchall()
    finally:
        conn.close()
    active = {str(code) for code, disabled_at in rows if not disabled_at}
    disabled = {str(code) for code, disabled_at in rows if disabled_at}
    return active, disabled


def candidate_dirs() -> list[Path]:
    dirs = []
    for p in [DEFAULT_DIR, FALLBACK_DIR]:
        if p.exists() and p not in dirs:
            dirs.append(p)
    return dirs


def cleanup(max_age_days: int, max_total_mb: int, dry_run: bool = False) -> dict:
    now = time.time()
    cutoff = now - max_age_days * 24 * 60 * 60
    active, disabled = load_codes(DEFAULT_DB)
    removed = []
    errors = []
    files = []
    for directory in candidate_dirs():
        for path in directory.iterdir():
            if not path.is_file() or path.suffix.lower() not in VALID_EXTS:
                continue
            code = path.stem
            try:
                stat = path.stat()
            except OSError as exc:
                errors.append(f"stat failed {path}: {exc}")
                continue
            orphan = code not in active and code not in disabled
            should_remove = orphan or code in disabled or stat.st_mtime < cutoff
            files.append((path, stat.st_size, stat.st_mtime, should_remove))
    for path, size, mtime, should_remove in files:
        if should_remove:
            if not dry_run:
                try:
                    path.unlink()
                except OSError as exc:
                    errors.append(f"remove failed {path}: {exc}")
                    continue
            removed.append(str(path))
    # Enforce total cap by deleting oldest remaining files.
    remaining = []
    for path, size, mtime, _ in files:
        if str(path) in removed or not path.exists():
            continue
        remaining.append((path, size, mtime))
    total = sum(size for _, size, _ in remaining)
    cap = max_total_mb * 1024 * 1024
    if max_total_mb > 0 and total > cap:
        for path, size, mtime in sorted(remaining, key=lambda item: item[2]):
            if total <= cap:
                break
            if not dry_run:
                try:
                    path.unlink()
                except OSError as exc:
                    errors.append(f"remove failed {path}: {exc}")
                    continue
            removed.append(str(path))
            total -= size
    return {"removed": removed, "errors": errors, "dry_run": dry_run}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument("--max-total-mb", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = cleanup(args.max_age_days, args.max_total_mb, dry_run=args.dry_run)
    if result["errors"]:
        print("preview image cleanup errors:")
        for err in result["errors"]:
            print(f"- {err}")
        return 1
    if result["dry_run"]:
        print(f"preview image cleanup would remove {len(result['removed'])} files")
        for path in result["removed"][:20]:
            print(f"- {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
