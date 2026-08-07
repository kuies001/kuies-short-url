#!/usr/bin/env python3
from __future__ import annotations

"""Bounded, cron-friendly refresh worker for Threads and Instagram previews.

Normal runs intentionally write nothing to stdout. Cron can therefore deliver only
real worker failures or unhealthy backlog warnings without an LLM agent.
"""

import argparse
import os
import sys
import time

from app import (
    PREVIEW_READY_REFRESH_SECONDS,
    ShortURLStore,
    _preview_image_cache_path,
    create_app,
)

DEFAULT_DB = os.environ.get("SHORT_DB", "./data/shorturls.sqlite3")
DEFAULT_LIMIT = 20
DEFAULT_DELAY = 0.5
HEALTH_PENDING_THRESHOLD = 50
HEALTH_OVERDUE_THRESHOLD = 50
HEALTH_MISSING_IMAGE_THRESHOLD = 20


def run_worker(db_path: str, limit: int = DEFAULT_LIMIT, dry_run: bool = False, delay: float = DEFAULT_DELAY) -> dict:
    store = ShortURLStore(db_path)
    rows = store.preview_refresh_candidates(limit=limit)
    result = {"selected": len(rows), "refreshed": 0, "failed": 0}
    if dry_run:
        return result

    app = create_app(store, "https://u.kuies.tw", "", preview_warm_enabled=True)
    failures = []
    for index, row in enumerate(rows):
        try:
            app._warm_preview(row, force_image_refresh=True)
            result["refreshed"] += 1
        except Exception as exc:  # One broken or deleted source must not abort the batch.
            result["failed"] += 1
            error = f"{type(exc).__name__}: {str(exc)[:160]}"
            store.mark_preview_failure(row.get("code") or "", error)
            failures.append(row.get("code") or "unknown")
        if delay > 0 and index + 1 < len(rows):
            time.sleep(delay)

    if failures:
        print(f"預覽刷新異常：{len(failures)} 筆失敗（{','.join(failures[:5])}）")
    return result


def health_statistics(db_path: str, now: int | None = None) -> dict:
    store = ShortURLStore(db_path)
    now = int(time.time()) if now is None else int(now)
    ready_before = now - PREVIEW_READY_REFRESH_SECONDS
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT code, preview_status, preview_updated_at, preview_next_retry_at
              FROM urls
             WHERE disabled_at IS NULL
               AND (lower(target_url) LIKE 'https://threads.com/%'
                    OR lower(target_url) LIKE 'https://www.threads.com/%'
                    OR lower(target_url) LIKE 'https://threads.net/%'
                    OR lower(target_url) LIKE 'https://www.threads.net/%'
                    OR lower(target_url) LIKE 'https://instagram.com/%'
                    OR lower(target_url) LIKE 'https://www.instagram.com/%')
            """
        ).fetchall()

    stats = {
        "total_social": len(rows),
        "ready": 0,
        "fallback": 0,
        "profile_fallback": 0,
        "pending": 0,
        "overdue": 0,
        "missing_images": 0,
    }
    for row in rows:
        status = row["preview_status"] or "pending"
        if status in stats:
            stats[status] += 1
        else:
            stats["pending"] += 1
        if status == "ready" and int(row["preview_updated_at"] or 0) <= ready_before:
            stats["overdue"] += 1
        elif status in {"fallback", "profile_fallback", "pending"} and int(row["preview_next_retry_at"] or 0) <= now:
            stats["overdue"] += 1
        cached_path, _ = _preview_image_cache_path(row["code"])
        # Pending rows have not had their first warm attempt yet, so counting
        # their expected lack of an image again would duplicate the pending
        # backlog and make the health alert noisy during a controlled backfill.
        if status != "pending" and not cached_path:
            stats["missing_images"] += 1
    return stats


def run_health_check(db_path: str) -> dict:
    stats = health_statistics(db_path)
    alerts = []
    if stats["pending"] > HEALTH_PENDING_THRESHOLD:
        alerts.append(f"pending={stats['pending']}")
    if stats["overdue"] > HEALTH_OVERDUE_THRESHOLD:
        alerts.append(f"overdue={stats['overdue']}")
    if stats["missing_images"] > HEALTH_MISSING_IMAGE_THRESHOLD:
        alerts.append(f"missing_images={stats['missing_images']}")
    if alerts:
        print("預覽健康警示：" + " ".join(alerts))
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh due social preview metadata")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--health-check", action="store_true")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    args = parser.parse_args(argv)
    try:
        if args.health_check:
            run_health_check(args.db)
        else:
            run_worker(
                args.db,
                limit=max(1, min(args.limit, 200)),
                dry_run=args.dry_run,
                delay=max(0.0, min(args.delay, 10.0)),
            )
        return 0
    except Exception as exc:
        print(f"預覽 worker 崩潰：{type(exc).__name__}: {str(exc)[:180]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
