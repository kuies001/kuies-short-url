#!/usr/bin/env python3
"""靜默證據回報：Messenger 收到 503 後的重試行為（一次性排程用）。

讀取服務端爬蟲紀錄 ``data/crawler-views.jsonl``：只有在出現「公網 IP 的
Meta 類爬蟲收到 503」時才輸出摘要（watchdog pattern）；沒有證據時完全
靜默、exit 0。``--verbose`` 可強制顯示目前狀態（手動檢查用）。

本機測試流量（ip=lan）由分析層自動排除，不會誤判為 Messenger 重試。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_DEFAULT = os.path.join(ROOT, "data", "crawler-views.jsonl")


def load_worker():
    path = os.path.join(ROOT, "preview-refresh.py")
    spec = importlib.util.spec_from_file_location("preview_refresh_worker", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Messenger 503 retry evidence report")
    parser.add_argument("--log", default=LOG_DEFAULT)
    parser.add_argument("--verbose", action="store_true", help="即使沒有證據也顯示目前狀態")
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    sys.path.insert(0, ROOT)
    worker = load_worker()
    result = worker.analyze_crawler_retries(args.log)
    retryable = int(result.get("retryable_codes") or 0)
    if retryable <= 0 and not args.verbose:
        return 0
    retried = int(result.get("retried_codes") or 0)
    local_ignored = int(result.get("local_ignored") or 0)
    lines: list[str] = []
    if retryable <= 0:
        lines.append("短網址 503 重試證據：尚無公網 Meta 爬蟲收到 503 的紀錄；重試行為仍待真實流量驗證。")
    else:
        lines.append(f"短網址 503 重試證據：{retryable} 個短碼曾收到 Meta 爬蟲 503；其後再次來訪 {retried} 個。")
        if retried <= 0:
            lines.append("已有 503 樣本但尚未觀察到再訪。")
    for sample in (result.get("samples") or [])[:5]:
        lines.append(
            f"  {sample['code']}：{sample['attempts']} 次來訪、"
            f"{sample['gap_seconds']} 秒後再訪，後續 {sample['after']}"
        )
    lines.append(
        f"（統計：紀錄 {int(result.get('entries') or 0)} 筆、"
        f"公網 Meta {int(result.get('meta_hits') or 0)} 筆、已排除本機測試 {local_ignored} 筆）"
    )
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
