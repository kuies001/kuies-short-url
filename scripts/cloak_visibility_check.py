#!/usr/bin/env python
"""Logged-out visibility probe for Threads posts (CloakBrowser CDP 9242).

Run with a python that has playwright (e.g. the Hermes venv):

    <python> scripts/cloak_visibility_check.py <threads-post-url>

Prints exactly one JSON line:

    {"ok": true, "visibility": "public"|"restricted"|"missing"|"login-wall"|"unknown",
     "signals": {...}, "detail": "..."}

The probe renders the post page in the shared, already-running CloakBrowser
(no login state) and reports what a logged-out visitor can see. Used as a
privacy gate before publishing preview metadata that was obtained through the
logged-in browser: only consistently public posts may be published
automatically. "restricted" means Threads itself told a logged-out visitor
that the content is not open to everyone.
"""

from __future__ import annotations

import json
import re
import sys

CDP_URL = "http://127.0.0.1:9242"


def _generic_title(title: str) -> bool:
    normalized = re.sub(r"\s+", " ", (title or "").strip())
    if not normalized:
        return True
    if normalized.casefold() in {"threads", "threads • 登入", "threads • log in", "instagram", "登入", "log in"}:
        return True
    return bool(re.match(r"^(?:Threads|Instagram)\s*(?:上的|貼文|•|·|，)", normalized, re.I))


def _login_phrase(text: str) -> bool:
    return bool(re.search(r"使用你的\s*Instagram\s*登入|log\s*in\s*with\s*instagram", (text or ""), re.I))


def main() -> int:
    url = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if not url.startswith("https://"):
        print(json.dumps({"ok": False, "visibility": "error", "detail": "bad-url"}, ensure_ascii=False))
        return 2
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - env dependent
        print(json.dumps({"ok": False, "visibility": "error", "detail": f"import:{type(exc).__name__}"}, ensure_ascii=False))
        return 1
    data = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CDP_URL)
            ctx = browser.contexts[0] if browser.contexts else None
            if ctx is None:
                print(json.dumps({"ok": False, "visibility": "error", "detail": "no-context"}, ensure_ascii=False))
                return 1
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=22000)
                page.wait_for_timeout(3000)
                data = page.evaluate(
                    """() => {
                        const body = document.body ? String(document.body.innerText || '') : '';
                        const lines = body.split(/\\n+/).map((l) => l.trim()).filter(Boolean);
                        const meta = (sel) => { const el = document.querySelector(sel); return el ? (el.getAttribute('content') || '') : ''; };
                        return {
                            href: location.href,
                            docTitle: (document.title || '').slice(0, 200),
                            ogTitle: meta('meta[property="og:title"]').slice(0, 200),
                            ogDescription: meta('meta[property="og:description"]').slice(0, 600),
                            restricted: body.includes('並未開放所有人查看') || body.includes('特定受眾無法查看') || body.includes('not available to everyone'),
                            notFound: body.includes('找不到') || body.includes("isn't available") || body.includes('無法使用'),
                            loginCta: body.includes('使用你的 Instagram 登入') || body.includes('Log in with Instagram'),
                            loginWord: lines.slice(0, 4).includes('登入') || lines.slice(0, 4).includes('Log in'),
                            lineCount: lines.length,
                            head: lines.slice(0, 12)
                        };
                    }"""
                )
            finally:
                try:
                    page.close()
                except Exception:
                    pass
    except Exception as exc:
        print(json.dumps({"ok": False, "visibility": "error", "detail": f"{type(exc).__name__}"}, ensure_ascii=False))
        return 1

    href = str(data.get("href") or "")
    want_parts = [p for p in url.split("://", 1)[-1].split("?", 1)[0].split("/") if p]
    got_parts = [p for p in href.split("://", 1)[-1].split("?", 1)[0].split("/") if p] if href else []
    redirected_away = bool(href) and want_parts[:3] != got_parts[:3]

    def content_visible() -> bool:
        og_title = str(data.get("ogTitle") or "")
        og_desc = str(data.get("ogDescription") or "")
        doc_title = str(data.get("docTitle") or "").strip()
        if og_desc and not _login_phrase(og_desc):
            return True
        if og_title and not _generic_title(og_title):
            return True
        if doc_title and not _generic_title(doc_title) and len(doc_title) >= 8:
            return True
        return False

    login_signal = bool(data.get("loginCta")) or _login_phrase(str(data.get("ogDescription") or ""))
    if data.get("restricted"):
        visibility = "restricted"
    elif data.get("notFound") or redirected_away:
        visibility = "missing"
    elif login_signal:
        visibility = "login-wall"
    elif content_visible():
        visibility = "public"
    elif data.get("loginWord"):
        visibility = "login-wall"
    else:
        visibility = "unknown"
    result = {
        "ok": True,
        "visibility": visibility,
        "signals": {
            "restricted": bool(data.get("restricted")),
            "notFound": bool(data.get("notFound")),
            "loginCta": bool(data.get("loginCta")),
            "loginWord": bool(data.get("loginWord")),
            "lineCount": data.get("lineCount"),
            "ogTitle": (data.get("ogTitle") or "")[:160],
            "docTitle": (data.get("docTitle") or "")[:160],
        },
        "detail": " | ".join(str(x) for x in (data.get("head") or [])[:8])[:240],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
