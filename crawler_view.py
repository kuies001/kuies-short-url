#!/usr/bin/env python3
"""Crawler-view helpers: what do social crawlers actually receive?

Used by the daily health check and the repair tool to verify a short code from
the outside (as facebookexternalhit would see it) and to classify the result
into ok / profile / degraded / retryable / error buckets.
"""

from __future__ import annotations

import re
from urllib.error import HTTPError
from urllib.request import Request, urlopen

META_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
HEALTH_UA = META_UA + " kuies-preview-health/1.0"

GENERIC_TITLES = {
    "",
    "threads",
    "threads 貼文",
    "instagram",
    "instagram 貼文",
    "threads 分享連結",
    "instagram 分享連結",
    "已移除追蹤參數的分享連結",
}


def _meta(html_text: str, key: str) -> str:
    patterns = [
        rf'<meta\s+[^>]*(?:property|name)=["\']{re.escape(key)}["\'][^>]*content=["\']([^"\']*)["\'][^>]*>',
        rf'<meta\s+[^>]*content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{re.escape(key)}["\'][^>]*>',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text, re.I | re.S)
        if match:
            return match.group(1).strip()
    return ""


def fetch_view(base_url: str, code: str, timeout: float = 12.0, opener=None, user_agent: str = META_UA) -> dict:
    """Fetch one short code the way a social crawler would."""
    opener = opener or urlopen
    url = f"{base_url.rstrip('/')}/{code}"
    request = Request(url, headers={"User-Agent": user_agent, "Accept": "text/html,*/*;q=0.8"})
    try:
        with opener(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 0) or 0)
            body = response.read(256 * 1024).decode("utf-8", "replace")
    except HTTPError as exc:
        return {"status": int(exc.code or 0), "title": "", "description": "", "image": ""}
    except Exception:
        return {"status": 0, "title": "", "description": "", "image": ""}
    return {
        "status": status,
        "title": _meta(body, "og:title"),
        "description": _meta(body, "og:description"),
        "image": _meta(body, "og:image"),
    }


def probe_image_url(image_url: str, timeout: float = 6.0) -> bool:
    """True when an OG image URL answers with a real image response."""
    if not (image_url or "").strip():
        return False
    request = Request(image_url.strip(), headers={"User-Agent": HEALTH_UA, "Accept": "image/*"})
    try:
        with urlopen(request, timeout=timeout) as response:
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            return int(getattr(response, "status", 0) or 0) == 200 and content_type.startswith("image/")
    except Exception:
        return False


def classify_view(view: dict, image_probe=None, title: str = "") -> str:
    """Bucket one crawler response.

    ok        - a real post card (title + image, no generic placeholders)
    profile   - an honest author-card fallback (title "Threads 貼文｜…")
    degraded  - generic title, missing title/image, or an unreadable image
    retryable - server asked the crawler to retry (503)
    error     - no usable response
    """
    status = int(view.get("status") or 0)
    if status == 503:
        return "retryable"
    if status != 200:
        return "error"
    title = (title or view.get("title") or "").strip()
    image = (view.get("image") or "").strip()
    normalized = re.sub(r"\s+", " ", title).casefold()
    if normalized in GENERIC_TITLES:
        return "degraded"
    if not image:
        return "degraded"
    if image_probe is not None and not image_probe(image):
        return "degraded"
    if title.startswith("Threads 貼文｜") or title.startswith("Instagram 貼文｜"):
        return "profile"
    return "ok"
