#!/usr/bin/env python3
"""舊卡片補救工具：升級降級的社群預覽，並提供可重新分享的新短碼。

用法：
    python3.11 repair_preview.py list [--limit N]
    python3.11 repair_preview.py repair <code> [--apply]

預設為唯讀 dry-run。--apply 才會寫入資料庫：
- 重新取得合格的預覽資料（公開抓取優先；貼文對訪客公開但公開抓取
  被 Threads 阻擋時，改用已登入瀏覽器），升級舊碼；
- 為同一目標建立新短碼（cache-bust），讓 Messenger 重新抓取預覽。

限制：
- 只有未登入訪客可見（public）的貼文可以補救；受限與已刪除的貼文
  維持現狀，不將登入後才可見的內容公開發布。
- Messenger 聊天室中的舊卡片不會自動更新；補救只對未來的分享有效。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import time

from app import (
    ShortURLStore,
    _preview_image_cache_path,
    cache_preview_image,
    fetch_open_graph_metadata,
    is_social_preview_target,
    sanitize_preview_metadata,
    social_preview_quality_issues,
)

try:
    import browser_preview
except Exception:  # pragma: no cover - degraded environments keep the tool read-only
    browser_preview = None

DEFAULT_DB = os.environ.get("SHORT_DB", "./data/shorturls.sqlite3")
DEFAULT_BACKUP_DIR = os.environ.get("SHORT_BACKUP_DIR", "./data/backups")
BASE_URL = os.environ.get("SHORT_BASE_URL", "https://u.kuies.tw").rstrip("/")
DEGRADED_STATUSES = {"profile_fallback", "fallback"}
LONG_PENDING_SECONDS = 60 * 60


def public_metadata(target_url: str, *, fetcher=None) -> dict | None:
    """Try to fetch acceptable post metadata the public way; None when unusable."""
    fetcher = fetcher or fetch_open_graph_metadata
    metadata = sanitize_preview_metadata(target_url, fetcher(target_url) or {})
    if not metadata:
        return None
    if social_preview_quality_issues(target_url, metadata, require_image=False):
        return None
    if not (metadata.get("image") or "").strip():
        return None
    title = (metadata.get("title") or "").strip()
    if title.startswith("Threads 貼文｜") or title.startswith("Instagram 貼文｜"):
        return None
    return metadata


def diagnose(target_url: str, *, fetcher=None, probe=None) -> dict:
    """Decide how one target could be repaired.

    Returns {source, metadata, visibility, reason}:
    - source "public": public fetch already produced good metadata;
    - source "browser": post is public to logged-out visitors but the public
      fetch is blocked, so the logged-in browser may fetch it;
    - source "": not repairable; reason explains the verdict.
    """
    metadata = public_metadata(target_url, fetcher=fetcher)
    if metadata:
        return {"source": "public", "metadata": metadata, "visibility": "public", "reason": ""}
    if browser_preview is None:
        return {"source": "", "metadata": None, "visibility": "unavailable", "reason": "browser-module-unavailable"}
    probe = probe or browser_preview.check_logged_out_visibility
    try:
        result = probe(target_url) or {}
    except Exception:  # noqa: BLE001 - diagnostics must not crash the tool
        result = {}
    visibility = str(result.get("visibility") or "unknown")
    if visibility == "public":
        return {"source": "browser", "metadata": None, "visibility": visibility, "reason": ""}
    return {"source": "", "metadata": None, "visibility": visibility, "reason": f"gate-{visibility}"}


def _copy_preview_image(src_path: str, new_code: str) -> str:
    """Copy a cached preview image next to the source file; "" on failure."""
    ext = os.path.splitext(src_path)[1] or ".jpg"
    try:
        dst = os.path.join(os.path.dirname(src_path), f"{new_code}{ext}")
        shutil.copy2(src_path, dst)
        if os.path.getsize(dst) > 0:
            return dst
    except OSError:
        pass
    return ""


def _backup_db(store: ShortURLStore, code: str) -> str:
    """Snapshot the database before a write; "" when the snapshot fails."""
    try:
        os.makedirs(DEFAULT_BACKUP_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(DEFAULT_BACKUP_DIR, f"shorturls-pre-repair-{code}-{stamp}.sqlite3")
        with store.connect() as conn:
            dest = sqlite3.connect(path)
            try:
                conn.backup(dest)
            finally:
                dest.close()
        return path
    except Exception:  # noqa: BLE001 - backup problems must not block the repair
        return ""


def apply_repair(
    store: ShortURLStore,
    code: str,
    diagnosis: dict,
    *,
    browser_fetcher=None,
    backup: bool = True,
) -> dict:
    """Execute one repair: upgrade the old code and mint a fresh code."""
    row = store.lookup(code)
    if row is None:
        return {"ok": False, "error": "code-not-found"}
    target_url = (row.get("target_url") or "").strip()
    if not is_social_preview_target(target_url):
        return {"ok": False, "error": "not-social-target"}

    metadata = diagnosis.get("metadata")
    source = "repair"
    if metadata is None and diagnosis.get("source") == "browser":
        if browser_preview is None:
            return {"ok": False, "error": "browser-module-unavailable"}
        # Independent second visibility check right before the logged-in fetch:
        # a single flaky probe must not be the only guard between a restricted
        # post and publication.
        try:
            recheck = browser_preview.check_logged_out_visibility(target_url) or {}
        except Exception:  # noqa: BLE001
            recheck = {}
        recheck_visibility = str(recheck.get("visibility") or "unknown")
        if recheck_visibility != "public":
            return {"ok": False, "error": f"gate-recheck-{recheck_visibility}"}
        browser_fetcher = browser_fetcher or browser_preview.fetch_threads_preview
        try:
            result = browser_fetcher(target_url, gate_verdict="public")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"browser:{type(exc).__name__}"}
        if not result.get("ok"):
            return {"ok": False, "error": f"browser:{result.get('error') or 'failed'}"}
        metadata = sanitize_preview_metadata(target_url, result.get("metadata") or {})
        if not metadata or social_preview_quality_issues(target_url, metadata, require_image=False):
            return {"ok": False, "error": "browser-metadata-degraded"}
        source = "browser"
    if not metadata:
        return {"ok": False, "error": diagnosis.get("reason") or "no-metadata"}

    backup_path = _backup_db(store, code) if backup else ""

    cached_path, _ = cache_preview_image(code, metadata.get("image", ""), force=True)
    if not cached_path:
        return {"ok": False, "error": "image-download-failed", "backup": backup_path}
    store.update_preview_metadata(code, metadata, "ready", image_path=cached_path, source=source)

    new_row = store.create_url(target_url, source="repair")
    new_code = new_row["code"]
    new_image = _copy_preview_image(cached_path, new_code)
    if not new_image:
        store.delete_url(new_code)
        return {
            "ok": True,
            "old_code": code,
            "new_code": "",
            "warning": "舊碼已升級；新碼圖片建立失敗，已移除新碼。",
            "backup": backup_path,
        }
    store.update_preview_metadata(new_code, metadata, "ready", image_path=new_image, source="repair")
    return {
        "ok": True,
        "old_code": code,
        "new_code": new_code,
        "metadata": metadata,
        "backup": backup_path,
    }


def collect_degraded(store: ShortURLStore, limit: int = 30, now: int | None = None) -> list[dict]:
    """Rows whose social preview is degraded, ready-but-defective, or stuck pending."""
    now = int(time.time()) if now is None else int(now)
    limit = max(1, min(int(limit), 200))
    degraded: list[dict] = []
    with store.connect() as conn:
        rows = conn.execute("SELECT * FROM urls WHERE disabled_at IS NULL ORDER BY created_at DESC").fetchall()
    for raw_row in rows:
        row = dict(raw_row)
        target_url = (row.get("target_url") or "").strip()
        if not is_social_preview_target(target_url):
            continue
        status = row.get("preview_status") or "pending"
        cached_path, _ = _preview_image_cache_path(row.get("code") or "")
        metadata = {
            "title": row.get("preview_title") or "",
            "description": row.get("preview_description") or "",
            "image": "",
        }
        issues = social_preview_quality_issues(
            target_url, metadata, image_path=cached_path or "", require_image=True
        )
        stuck_pending = status == "pending" and int(row.get("created_at") or 0) <= now - LONG_PENDING_SECONDS
        if status in DEGRADED_STATUSES or (status == "ready" and issues) or stuck_pending:
            degraded.append(row)
            if len(degraded) >= limit:
                break
    return degraded


def _status_text(row: dict) -> str:
    return str(row.get("preview_status") or "pending")


def cmd_list(store: ShortURLStore, limit: int) -> int:
    rows = collect_degraded(store, limit=limit)
    if not rows:
        print("沒有降級短碼。")
        return 0
    print(f"降級短碼（{len(rows)} 筆）：")
    for row in rows:
        created = time.strftime("%Y-%m-%d", time.localtime(int(row.get("created_at") or 0)))
        title = (row.get("preview_title") or "").strip() or "（無標題）"
        print(f"  {row['code']}  {_status_text(row)}  {created}  {title}")
        print(f"      {row.get('target_url') or ''}")
    print("使用 repair <code> 診斷；repair <code> --apply 執行補救。")
    return 0


def cmd_repair(store: ShortURLStore, code: str, apply: bool) -> int:
    row = store.lookup(code)
    if row is None:
        print(f"找不到短碼 {code}。")
        return 1
    target_url = (row.get("target_url") or "").strip()
    if not is_social_preview_target(target_url):
        print(f"{code} 不是 Threads／Instagram 貼文短碼，無需補救。")
        return 1

    diagnosis = diagnose(target_url)
    print(f"短碼 {code} 診斷")
    print(f"  目標：{target_url}")
    print(f"  現況：{_status_text(row)}（{(row.get('preview_title') or '').strip() or '無標題'}）")
    print(f"  未登入可見性：{diagnosis['visibility']}")

    if not diagnosis["source"]:
        print(f"  結果：不補救——{_refusal_text(diagnosis['visibility'])}")
        return 1

    plan = "重新公開抓取預覽" if diagnosis["source"] == "public" else "以已登入瀏覽器抓取（貼文對訪客公開，公開抓取被阻擋）"
    print(f"  計畫：{plan}；成功後升級舊碼並建立新短碼")
    if not apply:
        print("  （dry-run；加上 --apply 執行）")
        return 0

    result = apply_repair(store, code, diagnosis)
    if not result.get("ok"):
        print(f"  結果：補救失敗（{result.get('error')}）")
        if result.get("backup"):
            print(f"  備份：{result['backup']}")
        return 1
    print(f"  升級：舊碼 {code} 已更新為 ready（來源：{diagnosis['source']}）")
    if result.get("new_code"):
        print(f"  新碼：{result['new_code']} → {BASE_URL}/{result['new_code']}")
    elif result.get("warning"):
        print(f"  注意：{result['warning']}")
    if result.get("backup"):
        print(f"  備份：{result['backup']}")
    print("  提醒：Messenger 聊天室中的舊卡片不會自動更新；請改用新連結重新分享。")
    return 0


def _refusal_text(visibility: str) -> str:
    if visibility == "restricted":
        return "Threads 未對訪客開放此貼文，受限貼文維持作者卡，不將登入後內容公開發布。"
    if visibility == "missing":
        return "貼文已刪除或無法存取，無法產生有效預覽。"
    if visibility == "login-wall":
        return "只能確認登入牆，無法證明貼文對訪客公開。"
    return f"無法確認貼文公開狀態（{visibility}）。"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="舊卡片補救工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="列出降級短碼（唯讀）")
    p_list.add_argument("--limit", type=int, default=30)
    p_list.add_argument("--db", default=DEFAULT_DB)

    p_repair = sub.add_parser("repair", help="診斷與補救單一短碼（預設 dry-run）")
    p_repair.add_argument("code")
    p_repair.add_argument("--apply", action="store_true", help="實際寫入資料庫")
    p_repair.add_argument("--db", default=DEFAULT_DB)

    args = parser.parse_args(argv)
    store = ShortURLStore(args.db)
    if args.command == "list":
        return cmd_list(store, args.limit)
    if args.command == "repair":
        return cmd_repair(store, args.code, args.apply)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
