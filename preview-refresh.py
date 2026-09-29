#!/usr/bin/env python3
from __future__ import annotations

"""Bounded, cron-friendly refresh worker for Threads and Instagram previews.

Normal runs intentionally write nothing to stdout. Cron can therefore deliver only
real worker failures or unhealthy backlog warnings without an LLM agent.
"""

import argparse
import json
import os
import sys
import time
from urllib.parse import quote
from urllib.request import Request, urlopen

from app import (
    PREVIEW_READY_REFRESH_SECONDS,
    ShortURLStore,
    _preview_image_cache_path,
    create_app,
    social_preview_quality_issues,
)
from crawler_view import HEALTH_UA, classify_view, fetch_view, probe_image_url

DEFAULT_DB = os.environ.get("SHORT_DB", "./data/shorturls.sqlite3")
DEFAULT_LIMIT = 20
DEFAULT_DELAY = 0.5
HEALTH_PENDING_THRESHOLD = 50
HEALTH_OVERDUE_THRESHOLD = 50
HEALTH_MISSING_IMAGE_THRESHOLD = 20
HEALTH_LONG_PENDING_SECONDS = 60 * 60
PUBLIC_BASE_URL = os.environ.get("SHORT_BASE_URL", "https://u.kuies.tw").rstrip("/")

# Crawler-view sampling: a small daily check of what social crawlers actually
# receive from our own short URLs. Verdicts are compared against the stored
# state so the job only speaks up when a card's state changes.
CRAWLER_STATE_NAME = "crawler-health-state.json"
CRAWLER_LOG_NAME = "crawler-health.jsonl"
CRAWLER_SAMPLE_RECENT = 6
CRAWLER_SAMPLE_ROTATE = 8
CRAWLER_SAMPLE_WATCH = 3
CRAWLER_REPORT_LIMIT = 10
CRAWLER_RECENT_SECONDS = 24 * 60 * 60
CRAWLER_OFFLINE_AGGREGATE = 3


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


def probe_public_preview_image(code: str, path: str, content_type: str, timeout: float = 5.0) -> bool:
    """Confirm the first-party image route is actually serving a successful image response."""
    if not code or not path:
        return False
    ext = os.path.splitext(path)[1].lower()
    request = Request(
        f"{PUBLIC_BASE_URL}/preview-image/{quote(code)}{ext}",
        headers={"User-Agent": "kuies-short-url-preview-health/1.0", "Accept": "image/*"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            remote_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
            return int(response.status) == 200 and remote_type.startswith("image/")
    except Exception:
        return False


def health_statistics(db_path: str, now: int | None = None, image_probe=None) -> dict:
    store = ShortURLStore(db_path)
    now = int(time.time()) if now is None else int(now)
    image_probe = image_probe or probe_public_preview_image
    ready_before = now - PREVIEW_READY_REFRESH_SECONDS
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT code, target_url, created_at, preview_status, preview_title,
                   preview_description, preview_updated_at, preview_next_retry_at
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
        "broken_images": 0,
        "degraded_metadata": 0,
        "long_pending": 0,
        "repair_candidates": 0,
        "repair_codes": [],
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
        cached_path, content_type = _preview_image_cache_path(row["code"])
        metadata = {
            "title": row["preview_title"] or "",
            "description": row["preview_description"] or "",
            "image": "",
        }
        quality_issues = social_preview_quality_issues(
            row["target_url"], metadata, image_path=cached_path or "", require_image=True
        )
        metadata_issues = {
            "generic_or_login_title",
            "login_wall_metadata",
            "login_wall_image",
            "missing_text",
        }
        degraded_metadata = bool(metadata_issues.intersection(quality_issues))
        if degraded_metadata:
            stats["degraded_metadata"] += 1
        # Pending rows have not had their first warm attempt yet, so counting
        # their expected lack of an image again would duplicate the pending
        # backlog and make the health alert noisy during a controlled backfill.
        if status != "pending" and not cached_path:
            stats["missing_images"] += 1
        broken_image = bool(cached_path and not image_probe(row["code"], cached_path, content_type or ""))
        if broken_image:
            stats["broken_images"] += 1
        long_pending = status == "pending" and int(row["created_at"] or 0) <= now - HEALTH_LONG_PENDING_SECONDS
        if long_pending:
            stats["long_pending"] += 1
        if degraded_metadata or (status != "pending" and not cached_path) or broken_image or long_pending:
            stats["repair_codes"].append(row["code"])
    stats["repair_codes"] = list(dict.fromkeys(stats["repair_codes"]))
    stats["repair_candidates"] = len(stats["repair_codes"])
    return stats


def _load_crawler_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {"cursor": 0, "verdicts": {}}


def _save_crawler_state(path: str, state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
        os.replace(tmp_path, path)
    except OSError:
        pass


def _append_crawler_log(path: str, entries: list[dict]) -> None:
    if not entries:
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def crawler_view_check(
    db_path: str,
    base_url: str = PUBLIC_BASE_URL,
    now: int | None = None,
    fetcher=None,
    image_probe=None,
    state_path: str | None = None,
    log_path: str | None = None,
) -> dict:
    """Sample short codes the way a social crawler sees them; report only changes.

    Fetches our own public short URLs (never the Threads source page) with the
    health-marked Meta user agent, so the service can tell these requests apart
    from real Messenger crawls. A small rotating sample keeps the daily check
    cheap; verdicts are stored and only changes (or first sightings of a
    broken ready card) are reported, so silent degradation becomes visible
    without flooding the alert channel.
    """
    now = int(time.time()) if now is None else int(now)
    data_dir = os.path.dirname(os.path.abspath(db_path))
    state_path = state_path or os.path.join(data_dir, CRAWLER_STATE_NAME)
    log_path = log_path or os.path.join(data_dir, CRAWLER_LOG_NAME)
    fetcher = fetcher or (lambda code: fetch_view(base_url, code, user_agent=HEALTH_UA))
    image_probe = image_probe if image_probe is not None else probe_image_url

    state = _load_crawler_state(state_path)
    verdicts = dict(state.get("verdicts") or {})
    store = ShortURLStore(db_path)
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT code, target_url, preview_status, created_at FROM urls
             WHERE disabled_at IS NULL
               AND (lower(target_url) LIKE 'https://threads.com/%'
                    OR lower(target_url) LIKE 'https://www.threads.com/%'
                    OR lower(target_url) LIKE 'https://threads.net/%'
                    OR lower(target_url) LIKE 'https://www.threads.net/%'
                    OR lower(target_url) LIKE 'https://instagram.com/%'
                    OR lower(target_url) LIKE 'https://www.instagram.com/%')
             ORDER BY created_at DESC
            """
        ).fetchall()
    eligible = [dict(row) for row in rows if (row["preview_status"] or "") in {"ready", "profile_fallback"}]

    recent = [row for row in eligible if int(row["created_at"] or 0) >= now - CRAWLER_RECENT_SECONDS]
    recent = recent[:CRAWLER_SAMPLE_RECENT]
    picked = {row["code"] for row in recent}
    others = [row for row in eligible if row["code"] not in picked]
    cursor = int(state.get("cursor") or 0)
    rotate: list[dict] = []
    if others:
        total = len(others)
        take = min(CRAWLER_SAMPLE_ROTATE, total)
        rotate = [others[(cursor + index) % total] for index in range(take)]
        cursor = (cursor + take) % total
        picked.update(row["code"] for row in rotate)
    watch = [
        row
        for row in eligible
        if verdicts.get(row["code"]) in {"degraded", "error", "retryable"} and row["code"] not in picked
    ][:CRAWLER_SAMPLE_WATCH]
    sample = recent + rotate + watch

    reported: list[dict] = []
    entries: list[dict] = []
    offline_errors = 0
    for row in sample:
        code = row["code"]
        try:
            view = fetcher(code) or {}
        except Exception:  # noqa: BLE001 - one bad fetch must not abort the sample
            view = {"status": 0, "title": "", "description": "", "image": ""}
        if int(view.get("status") or 0) == 0:
            offline_errors += 1
        verdict = classify_view(view, image_probe=image_probe)
        previous = verdicts.get(code)
        db_status = row["preview_status"] or ""
        acceptable = {"ok"} if db_status == "ready" else {"profile", "ok"}
        changed = previous is not None and verdict != previous
        first_bad = previous is None and verdict not in acceptable
        entries.append(
            {"ts": now, "code": code, "db_status": db_status, "verdict": verdict, "previous": previous}
        )
        verdicts[code] = verdict
        if changed or first_bad:
            reported.append({"code": code, "db_status": db_status, "previous": previous, "verdict": verdict})

    if offline_errors >= CRAWLER_OFFLINE_AGGREGATE:
        # A public-entry outage would otherwise flood the report with per-code
        # errors; collapse them into one aggregate line instead.
        reported = [item for item in reported if item["verdict"] != "error"]

    state["cursor"] = cursor
    state["verdicts"] = verdicts
    _save_crawler_state(state_path, state)
    _append_crawler_log(log_path, entries)
    return {
        "sampled": len(sample),
        "reported": reported,
        "offline_errors": offline_errors,
        "state_path": state_path,
        "log_path": log_path,
    }


def _print_crawler_report(result: dict, verbose: bool = False) -> None:
    offline = int(result.get("offline_errors") or 0)
    reported = list(result.get("reported") or [])
    lines: list[str] = []
    if offline >= CRAWLER_OFFLINE_AGGREGATE:
        lines.append(f"爬蟲視角檢查：{offline} 筆無法連線（公開入口可能異常）")
    if reported:
        lines.append(f"爬蟲視角警示：{len(reported)} 筆狀態變化")
        for item in reported[:CRAWLER_REPORT_LIMIT]:
            previous = item.get("previous") or "首次檢查"
            lines.append(f"  {item.get('code')}（DB {item.get('db_status')}）：{previous} → {item.get('verdict')}")
        if len(reported) > CRAWLER_REPORT_LIMIT:
            lines.append(f"  …及其他 {len(reported) - CRAWLER_REPORT_LIMIT} 筆")
    if verbose and not lines:
        lines.append(f"爬蟲視角抽查：{int(result.get('sampled') or 0)} 筆，無異常")
    if lines:
        print("\n".join(lines))


def analyze_crawler_retries(log_path: str) -> dict:
    """Summarize whether social crawlers retried after a 503 (service-side log).

    Reads ``crawler-views.jsonl`` (written by the service for social short
    codes) and reports how many Meta-class crawler hits there were, which
    codes received a 503, and which were visited again afterwards — the
    evidence needed to decide whether Messenger retries a retryable card.
    """
    entries: list[dict] = []
    try:
        with open(log_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    entries.append(value)
    except OSError:
        entries = []
    meta = [entry for entry in entries if str(entry.get("ua") or "") == "meta"]
    by_code: dict[str, list[dict]] = {}
    for entry in meta:
        by_code.setdefault(str(entry.get("code") or ""), []).append(entry)
    retryable = 0
    retried: list[dict] = []
    for code, hits in by_code.items():
        statuses = [int(hit.get("status") or 0) for hit in hits]
        first_503 = next((index for index, status in enumerate(statuses) if status == 503), None)
        if first_503 is None:
            continue
        retryable += 1
        later = statuses[first_503 + 1:]
        if later:
            first_ts = int(hits[first_503].get("ts") or 0)
            next_ts = int(hits[first_503 + 1].get("ts") or 0)
            retried.append(
                {"code": code, "attempts": len(hits), "after": later, "gap_seconds": max(0, next_ts - first_ts)}
            )
    return {
        "entries": len(entries),
        "meta_hits": len(meta),
        "codes": len(by_code),
        "retryable_codes": retryable,
        "retried_codes": len(retried),
        "samples": retried[:10],
    }


def print_crawler_retries(result: dict) -> None:
    if not int(result.get("entries") or 0):
        print("爬蟲請求紀錄：尚無資料（crawler-views.jsonl 尚未寫入）。")
        return
    print(
        f"爬蟲請求紀錄：{result['entries']} 筆；Meta 類 {result['meta_hits']} 筆、{result['codes']} 個短碼。"
    )
    if not int(result.get("retryable_codes") or 0):
        print("尚無 Meta 爬蟲收到 503 的紀錄；重試行為仍待真實流量驗證。")
        return
    print(f"曾收到 503 的短碼：{result['retryable_codes']} 個；其後再次來訪：{result['retried_codes']} 個。")
    for sample in result.get("samples") or []:
        print(
            f"  {sample['code']}：{sample['attempts']} 次來訪，"
            f"{sample['gap_seconds']} 秒後再訪，後續 {sample['after']}"
        )


def run_health_check(db_path: str, crawler_fetcher=None) -> dict:
    stats = health_statistics(db_path)
    if stats["repair_codes"]:
        store = ShortURLStore(db_path)
        app = create_app(store, PUBLIC_BASE_URL, "", preview_warm_enabled=True)
        failures = []
        for code in stats["repair_codes"][:DEFAULT_LIMIT]:
            row = store.lookup(code)
            if not row:
                continue
            try:
                app._warm_preview(row, force_image_refresh=True)
            except Exception as exc:
                store.mark_preview_failure(code, f"health repair {type(exc).__name__}: {str(exc)[:160]}")
                failures.append(code)
        if failures:
            print(f"預覽健康修復異常：{len(failures)} 筆失敗（{','.join(failures[:5])}）")
    alerts = []
    if stats["pending"] > HEALTH_PENDING_THRESHOLD:
        alerts.append(f"pending={stats['pending']}")
    if stats["overdue"] > HEALTH_OVERDUE_THRESHOLD:
        alerts.append(f"overdue={stats['overdue']}")
    if stats["missing_images"] > HEALTH_MISSING_IMAGE_THRESHOLD:
        alerts.append(f"missing_images={stats['missing_images']}")
    if stats["broken_images"] > HEALTH_MISSING_IMAGE_THRESHOLD:
        alerts.append(f"broken_images={stats['broken_images']}")
    if stats["degraded_metadata"] > HEALTH_MISSING_IMAGE_THRESHOLD:
        alerts.append(f"degraded_metadata={stats['degraded_metadata']}")
    if stats["long_pending"] > HEALTH_PENDING_THRESHOLD:
        alerts.append(f"long_pending={stats['long_pending']}")
    if alerts:
        print("預覽健康警示：" + " ".join(alerts))
    try:
        crawler_result = crawler_view_check(db_path, fetcher=crawler_fetcher)
        _print_crawler_report(crawler_result)
    except Exception as exc:  # noqa: BLE001 - a broken probe must not kill the health job
        print(f"爬蟲視角檢查異常：{type(exc).__name__}: {str(exc)[:160]}")
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh due social preview metadata")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--health-check", action="store_true")
    parser.add_argument("--crawler-check", action="store_true", help="抽查爬蟲視角並輸出變化（唯讀）")
    parser.add_argument("--crawler-retries", action="store_true", help="分析 Meta 爬蟲 503 後重試行為（唯讀）")
    parser.add_argument(
        "--crawler-log",
        default=os.environ.get("SHORT_CRAWLER_LOG", "./data/crawler-views.jsonl"),
        help="服務端爬蟲請求紀錄路徑",
    )
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    args = parser.parse_args(argv)
    try:
        if args.health_check:
            run_health_check(args.db)
        elif args.crawler_check:
            _print_crawler_report(crawler_view_check(args.db), verbose=True)
        elif args.crawler_retries:
            print_crawler_retries(analyze_crawler_retries(args.crawler_log))
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
