#!/usr/bin/env python3
"""kuies.tw self-hosted short URL service.

No third-party dependencies: Python stdlib HTTP server + SQLite.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlparse, urlunparse
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RESERVED_CODES = {
    "api",
    "downloads",
    "gkd",
    "go",
    "healthz",
    "meme",
    "og-image",
    "preview-image",
    "privacy",
    "report",
    "shorten",
    "sr",
    "surl",
    "uch",
    "ucl",
    "ucp",
}
DEFAULT_CODE_MIN_LENGTH = 3
DEFAULT_CODE_MAX_LENGTH = 5
DEFAULT_CODE_LENGTH = DEFAULT_CODE_MIN_LENGTH
PUBLIC_RATE_LIMIT_COUNT = 5
PUBLIC_EXTENSION_RATE_LIMIT_COUNT = 30
PUBLIC_RATE_LIMIT_WINDOW = 60
PUBLIC_DAILY_IP_LIMIT = 100
PUBLIC_FORM_TOKEN_MAX_AGE = 2 * 60 * 60
ALERT_RECENT_WINDOW_SECONDS = 5 * 60
ALERT_RECENT_COUNT_THRESHOLD = 100
ALERT_TOTAL_COUNT_THRESHOLD = 1000
ALERT_RECENT_COOLDOWN_SECONDS = 60 * 60
DEFAULT_ALERT_EMAIL = "alerts@example.com"
DEFAULT_ALERT_FROM = "short-url@example.com"
DEFAULT_SENDMAIL = "/usr/sbin/sendmail"
PREVIEW_CRAWLER_RE = re.compile(
    r"facebookexternalhit|facebot|messengerbot|meta-externalagent|meta-externalfetcher|"
    r"telegrambot|twitterbot|discordbot|slackbot|linkedinbot|whatsapp",
    re.I,
)
LOCAL_PREVIEW_IMAGE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "preview-images"
)
# Keep external storage opt-in. A mounted but unhealthy exFAT directory can block
# directory enumeration for an hour, so the safe default is the local capped cache.
PREVIEW_IMAGE_DIR = os.environ.get("SHORT_PREVIEW_IMAGE_DIR", LOCAL_PREVIEW_IMAGE_DIR)
PREVIEW_READY_REFRESH_SECONDS = 7 * 24 * 60 * 60
PREVIEW_RETRY_BASE_SECONDS = 60 * 60
PREVIEW_RETRY_MAX_SECONDS = 7 * 24 * 60 * 60
PREVIEW_VERSION_LENGTH = 12
FALLBACK_PREVIEW_IMAGE_DIR = LOCAL_PREVIEW_IMAGE_DIR
MAX_PREVIEW_IMAGE_BYTES = 5 * 1024 * 1024
ABUSE_ALERT_WINDOW_SECONDS = 10 * 60
ABUSE_ALERT_COUNT_THRESHOLD = 20
ABUSE_ALERT_COOLDOWN_SECONDS = 60 * 60
RISK_KEYWORDS_RE = re.compile(
    r"login|signin|sign-in|verify|verification|password|passwd|reset|account|secure|security|wallet|bank|banking|otp|2fa|mfa|auth|authenticate|confirm|unlock|recover|recovery|gift|airdrop|crypto",
    re.I,
)
TRACKING_PARAM_RE = re.compile(r"^(utm_|fbclid$|gclid$|igsh$|xmt$)", re.I)
BLOCKED_SHORTENER_HOSTS = {
    "bit.ly",
    "tinyurl.com",
    "reurl.cc",
    "ppt.cc",
    "is.gd",
    "t.co",
    "goo.gl",
    "ow.ly",
    "buff.ly",
    "cutt.ly",
    "rb.gy",
    "shorturl.at",
    "load.tw",
}


def make_code(length: int = DEFAULT_CODE_LENGTH) -> str:
    """Generate a compact URL-safe code without underscore."""
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def is_safe_url(url: str) -> bool:
    """Allow only http(s) absolute URLs with a hostname."""
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _normalized_host(url: str) -> str:
    try:
        host = (urlparse(url.strip()).hostname or "").strip().lower().rstrip(".")
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _host_matches(host: str, blocked_host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    blocked_host = (blocked_host or "").lower().rstrip(".")
    return host == blocked_host or host.endswith(f".{blocked_host}")


def is_blocked_target_url(url: str, base_url: str = "") -> tuple[bool, str]:
    """Reject targets that would turn the shortener into an internal probe or shortener trampoline."""
    if not is_safe_url(url):
        return True, "只允許 http:// 或 https:// 開頭的有效網址"
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    normalized = host[4:] if host.startswith("www.") else host
    if not host:
        return True, "網址缺少有效主機名稱"
    if host in {"localhost", "localhost.localdomain"} or normalized.endswith(".local"):
        return True, "不允許縮短 localhost 或 .local 內網網址"
    if "." not in host and not re.match(r"^\[[0-9a-f:]+\]$", host, re.I):
        return True, "不允許縮短內網主機名稱"
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip = None
    if ip and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
        return True, "不允許縮短內網、保留或本機 IP 網址"
    base_host = _normalized_host(base_url)
    if base_host and _host_matches(normalized, base_host):
        return True, "不允許縮短本服務自己的網址，避免短網址循環"
    if any(_host_matches(normalized, blocked) for blocked in BLOCKED_SHORTENER_HOSTS):
        return True, "不允許縮短其他短網址服務，避免被當成跳板"
    return False, ""


def validate_target_url(target_url: str, base_url: str = "") -> None:
    blocked, reason = is_blocked_target_url(target_url, base_url=base_url)
    if blocked:
        raise ValueError(reason)


def _remove_query_keys(url: str, keys_to_remove: set[str]) -> str:
    parsed = urlparse(url.strip())
    pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in keys_to_remove
    ]
    return urlunparse(parsed._replace(query=urlencode(pairs, doseq=True)))


def clean_threads_url(url: str) -> str:
    """Remove Threads share-tracking xmt query parameter, preserving other params."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host not in {"threads.com", "www.threads.com", "threads.net", "www.threads.net"}:
        return url.strip()
    return _remove_query_keys(url, {"xmt"})


def clean_tracking_url(url: str) -> str:
    """Remove known Threads and Instagram tracking params while preserving useful params."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    keys_to_remove: set[str] = set()
    if host in {"threads.com", "www.threads.com", "threads.net", "www.threads.net"}:
        keys_to_remove.update({"xmt", "slof"})
    if host in {"instagram.com", "www.instagram.com"}:
        keys_to_remove.update({"igsh", "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "utm_id"})
    if host in {"facebook.com", "www.facebook.com", "m.facebook.com"}:
        keys_to_remove.update({"fbclid", "mibextid", "rdid", "share_url", "__cft__[0]", "__tn__", "ref", "refsrc"})
    if not keys_to_remove:
        return url.strip()
    return _remove_query_keys(url, keys_to_remove)


def build_urlcheck_social_catalog(upstream: dict) -> dict:
    """Merge conservative social tracking rules into a ClearURLs v2 catalog.

    URLCheck replaces top-level catalog objects when updating. Returning the
    complete upstream catalog preserves every official provider instead of
    silently replacing them with only the social additions.
    """
    catalog = copy.deepcopy(upstream)
    providers = catalog.get("providers")
    if not isinstance(providers, dict):
        raise ValueError("ClearURLs catalog must contain a providers object")

    additions = {
        "threads": {
            "urlPattern": r"^https?://(?:[a-z0-9-]+\.)*?threads\.(?:com|net)",
            "rules": ["xmt", "slof", "igsh", "igshid"],
        },
        "instagram": {
            "urlPattern": r"^https?://(?:[a-z0-9-]+\.)*?instagram\.com",
            "rules": ["igsh", "igshid"],
        },
        "facebook": {
            "urlPattern": r"^https?://(?:[a-z0-9-]+\.)*?facebook\.com",
            "rules": ["fbclid", "mibextid", "rdid", "share_url"],
        },
    }

    for name, extra in additions.items():
        provider = providers.setdefault(name, {})
        if not isinstance(provider, dict):
            raise ValueError(f"ClearURLs provider {name!r} must be an object")
        provider.setdefault("urlPattern", extra["urlPattern"])
        existing_rules = provider.get("rules", [])
        if not isinstance(existing_rules, list):
            raise ValueError(f"ClearURLs provider {name!r} rules must be an array")
        provider["rules"] = list(dict.fromkeys([*existing_rules, *extra["rules"]]))

    return catalog


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def resolve_threads_share_url(url: str, timeout: float = 2.0, opener=None) -> str:
    """Resolve Threads /share/<token> to a clean canonical post URL."""
    source = url.strip()
    parsed = urlparse(source)
    threads_hosts = {"threads.com", "www.threads.com", "threads.net", "www.threads.net"}
    if (parsed.hostname or "").lower() not in threads_hosts:
        return source
    if not re.fullmatch(r"/share/[A-Za-z0-9_-]+/?", parsed.path):
        return source

    request = Request(
        source,
        method="HEAD",
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; kuies.tw Threads share resolver/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    redirect_location = ""
    opener = opener or build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            redirect_location = response.headers.get("Location", "") or response.geturl()
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            redirect_location = exc.headers.get("Location", "")
    except Exception:
        return source

    target = urlparse(redirect_location)
    if target.scheme.lower() != "https" or (target.hostname or "").lower() not in threads_hosts:
        return source
    if not re.fullmatch(r"/@[^/]+/post/[^/?#]+/?", target.path):
        return source
    return urlunparse(("https", "www.threads.com", target.path.rstrip("/"), "", "", ""))


def resolve_facebook_share_url(url: str, timeout: float = 2.0, opener=None) -> str:
    """Resolve Facebook /share/... wrappers to a cleaned same-site post URL."""
    source = url.strip()
    parsed = urlparse(source)
    facebook_hosts = {"facebook.com", "www.facebook.com", "m.facebook.com"}
    if (parsed.hostname or "").lower() not in facebook_hosts:
        return source
    if not re.fullmatch(r"/share(?:/[prv])?/[A-Za-z0-9_-]+/?", parsed.path):
        return source

    request = Request(
        source,
        method="HEAD",
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; kuies.tw Facebook share resolver/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    redirect_location = ""
    opener = opener or build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            redirect_location = response.headers.get("Location", "") or response.geturl()
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            redirect_location = exc.headers.get("Location", "")
    except Exception:
        return source

    target = urlparse(redirect_location)
    if target.scheme.lower() != "https" or (target.hostname or "").lower() not in facebook_hosts:
        return source
    if re.fullmatch(r"/share(?:/[prv])?/[A-Za-z0-9_-]+/?", target.path):
        return source
    normalized = urlunparse(("https", "www.facebook.com", target.path, "", target.query, ""))
    return clean_tracking_url(normalized)


def is_supported_social_share_wrapper(url: str) -> bool:
    """Return whether URL is an opaque Facebook/Threads share wrapper we can safely resolve."""
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() != "https" or parsed.username or parsed.password or parsed.port:
        return False
    host = (parsed.hostname or "").lower()
    if host in {"facebook.com", "www.facebook.com", "m.facebook.com"}:
        return bool(re.fullmatch(r"/share(?:/[prv])?/[A-Za-z0-9_-]+/?", parsed.path))
    if host in {"threads.com", "www.threads.com", "threads.net", "www.threads.net"}:
        return bool(re.fullmatch(r"/share/[A-Za-z0-9_-]+/?", parsed.path))
    return False


def prepare_target_url(url: str, clean_threads_xmt: bool = False, clean_tracking: bool = False) -> str:
    url = url.strip()
    if clean_tracking:
        url = resolve_threads_share_url(url)
        url = resolve_facebook_share_url(url)
        return clean_tracking_url(url)
    return clean_threads_url(url) if clean_threads_xmt else url

def preview_image_dir() -> str:
    """Use external disk for preview images when writable, otherwise fallback locally."""
    for candidate in (PREVIEW_IMAGE_DIR, FALLBACK_PREVIEW_IMAGE_DIR):
        try:
            os.makedirs(candidate, exist_ok=True)
            test_path = os.path.join(candidate, ".write-test")
            with open(test_path, "wb") as fh:
                fh.write(b"")
            try:
                os.unlink(test_path)
            except OSError:
                pass
            return candidate
        except Exception:
            continue
    return FALLBACK_PREVIEW_IMAGE_DIR


def canonicalize_target_url(url: str) -> str:
    parsed = urlparse(clean_tracking_url(url.strip()))
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    port = parsed.port
    netloc = host
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{host}:{port}"
    path = quote(unquote(parsed.path or "/"), safe="/%:@")
    pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not TRACKING_PARAM_RE.match(k or "")
    ]
    query = urlencode(sorted(pairs), doseq=True)
    return urlunparse((scheme, netloc, path, "", query, ""))


def target_hash_for(url: str) -> str:
    return hashlib.sha256(canonicalize_target_url(url).encode("utf-8")).hexdigest()


def target_domain(url: str) -> str:
    return _normalized_host(url)


def risk_keywords_for_url(url: str) -> list[str]:
    parsed = urlparse(url.strip())
    haystack = " ".join([parsed.hostname or "", unquote(parsed.path or ""), unquote(parsed.query or "")])
    return sorted({m.group(0).lower() for m in RISK_KEYWORDS_RE.finditer(haystack)})


def needs_warning_page(url: str) -> bool:
    return bool(risk_keywords_for_url(url))


def is_private_client(remote_addr: str) -> bool:
    """Return True for loopback/private/link-local LAN clients."""
    try:
        ip = ipaddress.ip_address((remote_addr or "").strip())
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def is_preview_crawler(user_agent: str) -> bool:
    """Return True for Meta/Messenger link-preview crawlers."""
    return bool(PREVIEW_CRAWLER_RE.search(user_agent or ""))


def display_host(url: str) -> str:
    try:
        parsed = urlparse(url)
    except Exception:
        return "原始連結"
    host = (parsed.hostname or "原始連結").lower()
    if host.startswith("www."):
        host = host[4:]
    if "threads." in host:
        return "Threads"
    if "instagram." in host:
        return "Instagram"
    return host


def is_social_preview_target(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in {
        "threads.com",
        "www.threads.com",
        "threads.net",
        "www.threads.net",
        "instagram.com",
        "www.instagram.com",
    }


def is_safe_preview_target(url: str, base_url: str = "") -> bool:
    """Return True when it is safe to render an OG preview page for Meta/Messenger.

    Messenger often does not build a card from a bare 302 short URL even when Line/TG do.
    For crawler requests we therefore serve a tiny first-party OG page for any public target,
    while still refusing private/internal/self/shortener trampoline URLs.
    """
    blocked, _ = is_blocked_target_url(url, base_url=base_url)
    return not blocked


def _extract_meta(html_text: str, key: str) -> str:
    patterns = [
        rf'<meta\s+[^>]*(?:property|name)=["\']{re.escape(key)}["\'][^>]*content=["\']([^"\']*)["\'][^>]*>',
        rf'<meta\s+[^>]*content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{re.escape(key)}["\'][^>]*>',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text, re.I | re.S)
        if match:
            return html.unescape(re.sub(r"\s+", " ", match.group(1)).strip())
    return ""


def fetch_open_graph_metadata(url: str, timeout: int = 5) -> dict:
    """Fetch OG metadata from public targets for Messenger preview rendering."""
    if not is_safe_url(url):
        return {}
    request = Request(
        url,
        headers={
            "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(1_500_000)
            charset = response.headers.get_content_charset() or "utf-8"
    except Exception:
        return {}
    text = raw.decode(charset, errors="ignore")
    title = _extract_meta(text, "og:title") or _extract_meta(text, "twitter:title")
    description = _extract_meta(text, "og:description") or _extract_meta(text, "twitter:description")
    image = _extract_meta(text, "og:image") or _extract_meta(text, "twitter:image")
    if not title:
        title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        if title_match:
            title = html.unescape(re.sub(r"\s+", " ", title_match.group(1)).strip())
    return {"title": title, "description": description, "image": image}


def is_social_login_metadata(host_label: str, metadata: dict) -> bool:
    """Detect login-wall OG metadata so we do not publish it as the short URL preview."""
    if host_label not in {"Threads", "Instagram"}:
        return False
    title = re.sub(r"\s+", " ", (metadata.get("title") or "").strip()).casefold()
    description = re.sub(r"\s+", " ", (metadata.get("description") or "").strip()).casefold()
    login_title = bool(
        re.fullmatch(
            r"(?:(?:threads|instagram)\s*(?:[•·|｜:：-]\s*)?)?(?:log\s*in|login|登入)(?:\s*[•·|｜].*)?",
            title,
            re.I,
        )
    )
    login_description = bool(
        re.search(
            r"(?:use|using)\s+(?:your\s+)?instagram\s+(?:account\s+)?to\s+log\s*in|"
            r"log\s*in\s+(?:with|using)\s+(?:your\s+)?instagram|使用你的\s*instagram\s*登入",
            description,
            re.I,
        )
    )
    return login_title or login_description


def is_social_login_image_url(image_url: str) -> bool:
    """Reject known Threads/Instagram login-wall artwork independently of its title."""
    image = (image_url or "").strip().lower()
    if not image:
        return False
    try:
        host = (urlparse(image).hostname or "").lower()
    except ValueError:
        return True
    return host == "static.cdninstagram.com" or host.endswith(".static.cdninstagram.com")


def _is_generic_or_login_social_title(host_label: str, title: str) -> bool:
    normalized = re.sub(r"\s+", " ", (title or "").strip()).casefold()
    generic = {
        host_label.casefold(),
        f"{host_label} 貼文".casefold(),
    }
    return normalized in generic or is_social_login_metadata(host_label, {"title": title})


def social_preview_quality_issues(
    target_url: str,
    metadata: dict,
    image_path: str = "",
    require_image: bool = True,
) -> list[str]:
    """Return stable quality defects used by warmup, refresh and health checks."""
    if not is_social_preview_target(target_url):
        return []
    host_label = display_host(target_url)
    title = (metadata.get("title") or "").strip()
    description = (metadata.get("description") or "").strip()
    image_url = (metadata.get("image") or "").strip()
    issues: list[str] = []
    if _is_generic_or_login_social_title(host_label, title):
        issues.append("generic_or_login_title")
    if is_social_login_metadata(host_label, metadata):
        issues.append("login_wall_metadata")
    if is_social_login_image_url(image_url):
        issues.append("login_wall_image")
    if not title and not description:
        issues.append("missing_text")
    if require_image and not (image_path and os.path.isfile(image_path) and os.path.getsize(image_path) > 0):
        issues.append("missing_image")
    return list(dict.fromkeys(issues))


def sanitize_preview_metadata(target_url: str, metadata: dict) -> dict:
    """Drop misleading social login-wall metadata from Threads/Instagram public pages."""
    host_label = display_host(target_url)
    if is_social_login_metadata(host_label, metadata):
        return {}
    sanitized = dict(metadata)
    if host_label in {"Threads", "Instagram"} and is_social_login_image_url(sanitized.get("image") or ""):
        sanitized["image"] = ""
    return sanitized


def fetch_social_profile_fallback_metadata(target_url: str) -> dict:
    """Use a Threads profile card when an individual post cannot be read publicly.

    Threads occasionally redirects a valid-looking post URL to ``invalid_post`` or
    serves a login wall to crawlers.  Returning only ``Threads 貼文`` makes Messenger
    cache a useless card.  A public profile still provides a stable author name,
    description and image without pretending that we recovered the missing post text.
    """
    try:
        parsed = urlparse(target_url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
        post_match = re.fullmatch(r"/@([A-Za-z0-9._]+)/post/[^/?#]+/?", parsed.path)
    except (TypeError, ValueError):
        return {}
    if (
        parsed.scheme.lower() != "https"
        or parsed.username
        or parsed.password
        or port
        or host not in {"threads.com", "www.threads.com", "threads.net", "www.threads.net"}
        or not post_match
    ):
        return {}
    handle = f"@{post_match.group(1)}"
    # Never reuse scheme/netloc from stored input: the fallback may only fetch this
    # canonical public Threads origin, not userinfo, custom ports or another domain.
    profile_url = f"https://www.threads.com/{handle}"
    profile = sanitize_preview_metadata(profile_url, fetch_open_graph_metadata(profile_url))
    if not any((profile.get(key) or "").strip() for key in ("title", "description", "image")):
        return {}
    raw_title = (profile.get("title") or "").strip()
    profile_name = re.split(r"\s*[•·]\s*Threads\b", raw_title, maxsplit=1, flags=re.I)[0].strip()
    if not profile_name:
        profile_name = handle
    return {
        "title": f"Threads 貼文｜{profile_name}",
        "description": (profile.get("description") or "").strip(),
        "image": (profile.get("image") or "").strip(),
    }


def _preview_image_extension(content_type: str, image_url: str = "") -> tuple[str, str]:
    content_type = (content_type or "").split(";", 1)[0].strip().lower()
    if content_type in {"image/jpeg", "image/jpg"}:
        return ".jpg", "image/jpeg"
    if content_type == "image/png":
        return ".png", "image/png"
    if content_type == "image/webp":
        return ".webp", "image/webp"
    path = urlparse(image_url).path.lower()
    if path.endswith((".jpg", ".jpeg")):
        return ".jpg", "image/jpeg"
    if path.endswith(".png"):
        return ".png", "image/png"
    if path.endswith(".webp"):
        return ".webp", "image/webp"
    return ".jpg", "image/jpeg"


def _preview_image_cache_path(code: str) -> tuple[str, str] | tuple[None, None]:
    if not SAFE_CODE_RE.match(code or ""):
        return None, None
    # Read both locations. The external volume can disappear and reappear while
    # launchd keeps the service alive, so valid cache files may be split across
    # the preferred external directory and the local fallback directory.
    directories = []
    for directory in (PREVIEW_IMAGE_DIR, FALLBACK_PREVIEW_IMAGE_DIR):
        absolute = os.path.abspath(directory)
        if absolute not in directories:
            directories.append(absolute)
    for directory in directories:
        for ext, content_type in [(".jpg", "image/jpeg"), (".png", "image/png"), (".webp", "image/webp")]:
            path = os.path.join(directory, f"{code}{ext}")
            if os.path.exists(path) and os.path.getsize(path) > 0:
                return path, content_type
    return None, None


def cache_preview_image(
    code: str,
    image_url: str,
    timeout: int = 8,
    force: bool = False,
) -> tuple[str, str] | tuple[None, None]:
    """Cache remote OG image under our own domain so Messenger can fetch it reliably."""
    if not (image_url or "").strip():
        return None, None
    cached_path, cached_type = _preview_image_cache_path(code)
    if cached_path and not force:
        return cached_path, cached_type
    if not SAFE_CODE_RE.match(code or ""):
        return None, None
    blocked, _ = is_blocked_target_url(image_url or "")
    if blocked:
        return None, None
    parsed = urlparse(image_url)
    if parsed.scheme != "https":
        return None, None
    os.makedirs(preview_image_dir(), exist_ok=True)
    request = Request(
        image_url,
        headers={
            "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Referer": "https://www.threads.com/",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            data = response.read(MAX_PREVIEW_IMAGE_BYTES + 1)
    except Exception:
        return None, None
    if len(data) > MAX_PREVIEW_IMAGE_BYTES or not data:
        return None, None
    ext, normalized_type = _preview_image_extension(content_type, image_url)
    if not normalized_type.startswith("image/"):
        return None, None
    image_dir = preview_image_dir()
    os.makedirs(image_dir, exist_ok=True)
    path = os.path.join(image_dir, f"{code}{ext}")
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(data)
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        return None, None
    return path, normalized_type


def preview_content_version(metadata: dict, image_path: str = "") -> str:
    """Hash public preview content so social image caches get a new immutable URL."""
    digest = hashlib.sha256()
    normalized = {
        "title": metadata.get("title") or "",
        "description": metadata.get("description") or "",
        "image": metadata.get("image") or "",
    }
    digest.update(json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    if image_path and os.path.isfile(image_path):
        try:
            with open(image_path, "rb") as fh:
                while chunk := fh.read(64 * 1024):
                    digest.update(chunk)
        except OSError:
            pass
    return digest.hexdigest()[:PREVIEW_VERSION_LENGTH]


def social_preview_fallback_title(target_url: str, host_label: str) -> str:
    """Build a URL-specific fallback so social caches never store the vague '分享連結' title."""
    handle = ""
    try:
        for part in urlparse(target_url).path.split("/"):
            if part.startswith("@") and len(part) > 1:
                handle = part
                break
    except Exception:
        pass
    return f"{host_label} 貼文｜{handle}" if handle else f"{host_label} 貼文"


def choose_preview_text(host_label: str, metadata: dict, fallback_title: str = "", target_url: str = "") -> tuple[str, str]:
    """Prefer post text as title when Threads/Instagram title is only author/account."""
    raw_title = (metadata.get("title") or fallback_title or "").strip()
    raw_description = (metadata.get("description") or "").strip()
    generic_stored_title = raw_title in {"已移除追蹤參數的分享連結"}
    if host_label in {"Threads", "Instagram"} and generic_stored_title:
        return social_preview_fallback_title(target_url, host_label), f"透過 kuies.tw 短網址前往 {host_label}。"
    generic_social_title = bool(re.search(r"^(Threads|Instagram)\s+上的|^Instagram\s+photo\s+by|^Threads\s+post\s+by", raw_title, re.I))
    if host_label in {"Threads", "Instagram"} and raw_description and generic_social_title:
        return raw_description, raw_title
    return raw_title, raw_description


class ShortURLStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS urls (
                    code TEXT PRIMARY KEY,
                    target_url TEXT NOT NULL,
                    title TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    clicks INTEGER NOT NULL DEFAULT 0,
                    last_clicked_at INTEGER,
                    source TEXT,
                    creator_ip TEXT,
                    disabled_at INTEGER,
                    disabled_reason TEXT,
                    canonical_url TEXT,
                    target_hash TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS clicks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    clicked_at INTEGER NOT NULL,
                    user_agent TEXT,
                    remote_addr TEXT,
                    FOREIGN KEY(code) REFERENCES urls(code)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_state (
                    key TEXT PRIMARY KEY,
                    last_sent_at INTEGER NOT NULL,
                    value INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._ensure_url_columns(conn)

    def _ensure_url_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(urls)").fetchall()}
        migrations = {
            "source": "ALTER TABLE urls ADD COLUMN source TEXT",
            "creator_ip": "ALTER TABLE urls ADD COLUMN creator_ip TEXT",
            "disabled_at": "ALTER TABLE urls ADD COLUMN disabled_at INTEGER",
            "disabled_reason": "ALTER TABLE urls ADD COLUMN disabled_reason TEXT",
            "canonical_url": "ALTER TABLE urls ADD COLUMN canonical_url TEXT",
            "target_hash": "ALTER TABLE urls ADD COLUMN target_hash TEXT",
            "preview_title": "ALTER TABLE urls ADD COLUMN preview_title TEXT",
            "preview_description": "ALTER TABLE urls ADD COLUMN preview_description TEXT",
            "preview_status": "ALTER TABLE urls ADD COLUMN preview_status TEXT",
            "preview_updated_at": "ALTER TABLE urls ADD COLUMN preview_updated_at INTEGER",
            "preview_failure_count": "ALTER TABLE urls ADD COLUMN preview_failure_count INTEGER NOT NULL DEFAULT 0",
            "preview_next_retry_at": "ALTER TABLE urls ADD COLUMN preview_next_retry_at INTEGER",
            "preview_last_error": "ALTER TABLE urls ADD COLUMN preview_last_error TEXT",
            "preview_version": "ALTER TABLE urls ADD COLUMN preview_version TEXT",
        }
        for column, statement in migrations.items():
            if column not in existing:
                conn.execute(statement)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_urls_target_hash ON urls(target_hash)")
        rows = conn.execute("SELECT code, target_url FROM urls WHERE target_hash IS NULL OR canonical_url IS NULL").fetchall()
        for row in rows:
            canonical = canonicalize_target_url(row["target_url"])
            conn.execute(
                "UPDATE urls SET canonical_url = ?, target_hash = ? WHERE code = ?",
                (canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest(), row["code"]),
            )

    def suggest_codes(self, count: int = 3) -> list[str]:
        suggestions: list[str] = []
        attempts = 0
        while len(suggestions) < count and attempts < 200:
            attempts += 1
            code = make_code(DEFAULT_CODE_LENGTH)
            if code.lower() in RESERVED_CODES or code in suggestions or self.lookup(code) is not None:
                continue
            suggestions.append(code)
        return suggestions

    def _code_conflict_message(self, code: str, reason: str = "duplicate") -> str:
        suggestions = self.suggest_codes(3)
        suggestion_text = "、".join(suggestions)
        prefix = f"短碼「{code}」已被使用" if reason == "duplicate" else f"短碼「{code}」是系統保留路徑"
        if suggestion_text:
            return f"{prefix}，請重新輸入。建議短碼：{suggestion_text}；或將短碼欄位留空，由系統自動產生。"
        return f"{prefix}，請重新輸入；或將短碼欄位留空，由系統自動產生。"

    def create_url(
        self,
        target_url: str,
        code: Optional[str] = None,
        title: Optional[str] = None,
        source: str = "",
        creator_ip: str = "",
    ) -> dict:
        target_url = target_url.strip()
        if not is_safe_url(target_url):
            raise ValueError("只允許 http:// 或 https:// 開頭的有效網址")

        custom = code is not None and code != ""
        if custom:
            code = code.strip()
            if not SAFE_CODE_RE.match(code):
                raise ValueError("短碼只能使用英數字、底線、連字號，長度 1-64")
            if code.lower() in RESERVED_CODES:
                raise ValueError(self._code_conflict_message(code, reason="reserved"))
            if self.lookup(code) is not None:
                raise ValueError(self._code_conflict_message(code, reason="duplicate"))
        else:
            code = self._unique_code()

        now = int(time.time())
        canonical_url = canonicalize_target_url(target_url)
        target_hash = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
        try:
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO urls(code, target_url, title, created_at, updated_at, source, creator_ip, canonical_url, target_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (code, target_url, title, now, now, source[:64] or None, creator_ip[:128] or None, canonical_url, target_hash),
                )
        except sqlite3.IntegrityError:
            raise ValueError(self._code_conflict_message(code or "", reason="duplicate"))
        return self.lookup(code)

    def _unique_code(self) -> str:
        for length in range(DEFAULT_CODE_MIN_LENGTH, DEFAULT_CODE_MAX_LENGTH + 1):
            for _ in range(50):
                code = make_code(length)
                if code.lower() in RESERVED_CODES:
                    continue
                if self.lookup(code) is None:
                    return code
        raise RuntimeError("無法產生唯一短碼，請稍後再試")

    def lookup(self, code: str, count_click: bool = False) -> Optional[dict]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM urls WHERE code = ?", (code,)).fetchone()
        if row is None:
            return None
        return dict(row)

    def list_urls(self, limit: int = 100) -> list:
        limit = max(1, min(limit, 500))
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM urls ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def count_urls(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM urls").fetchone()
        return int(row["count"])

    def count_urls_since(self, since_ts: int) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM urls WHERE created_at >= ?", (since_ts,)).fetchone()
        return int(row["count"])

    def count_urls_by_creator_ip_since(self, creator_ip: str, since_ts: int) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM urls WHERE creator_ip = ? AND created_at >= ?",
                ((creator_ip or "")[:128], since_ts),
            ).fetchone()
        return int(row["count"])

    def find_by_target_hash(self, target_hash: str) -> Optional[dict]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM urls WHERE target_hash = ? ORDER BY created_at ASC LIMIT 1", (target_hash,)).fetchone()
        return dict(row) if row else None

    def count_urls_by_domain_since(self, domain: str, since_ts: int) -> int:
        pattern = f"%://%{domain}%"
        with self.connect() as conn:
            rows = conn.execute("SELECT target_url FROM urls WHERE created_at >= ?", (since_ts,)).fetchall()
        return sum(1 for row in rows if target_domain(row["target_url"]) == domain)

    def recent_domains_since(self, since_ts: int) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute("SELECT target_url FROM urls WHERE created_at >= ?", (since_ts,)).fetchall()
        counts = {}
        for row in rows:
            domain = target_domain(row["target_url"])
            if domain:
                counts[domain] = counts.get(domain, 0) + 1
        return counts

    def set_disabled(self, code: str, disabled: bool = True, reason: str = "") -> bool:
        now = int(time.time()) if disabled else None
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE urls SET disabled_at = ?, disabled_reason = ?, updated_at = ? WHERE code = ?",
                (now, reason[:500] if disabled else None, int(time.time()), code),
            )
            return cur.rowcount > 0

    def update_preview_metadata(
        self,
        code: str,
        metadata: dict,
        status: str,
        image_path: str = "",
        error: str = "",
    ) -> bool:
        now = int(time.time())
        with self.connect() as conn:
            previous = conn.execute(
                "SELECT preview_failure_count FROM urls WHERE code = ?", (code,)
            ).fetchone()
            if previous is None:
                return False
            is_ready = status == "ready"
            failure_count = 0 if is_ready else int(previous["preview_failure_count"] or 0) + 1
            retry_delay = min(
                PREVIEW_RETRY_BASE_SECONDS * (2 ** max(0, min(failure_count - 1, 20))),
                PREVIEW_RETRY_MAX_SECONDS,
            )
            next_retry_at = None if is_ready else now + retry_delay
            last_error = None if is_ready else ((error or status or "preview unavailable")[:500])
            version = preview_content_version(metadata, image_path=image_path)
            cur = conn.execute(
                """
                UPDATE urls
                   SET preview_title = ?, preview_description = ?, preview_status = ?,
                       preview_updated_at = ?, updated_at = ?, preview_failure_count = ?,
                       preview_next_retry_at = ?, preview_last_error = ?, preview_version = ?
                 WHERE code = ?
                """,
                (
                    (metadata.get("title") or "")[:2000],
                    (metadata.get("description") or "")[:5000],
                    (status or "fallback")[:64],
                    now,
                    now,
                    failure_count,
                    next_retry_at,
                    last_error,
                    version,
                    code,
                ),
            )
            return cur.rowcount > 0

    def mark_preview_failure(self, code: str, error: str) -> bool:
        row = self.lookup(code)
        if not row:
            return False
        metadata = {
            "title": row.get("preview_title") or "",
            "description": row.get("preview_description") or "",
            "image": "",
        }
        return self.update_preview_metadata(
            code,
            metadata,
            row.get("preview_status") or "fallback",
            error=error,
        )

    def preview_refresh_candidates(self, limit: int = 20, now: Optional[int] = None) -> list[dict]:
        """Return pending, degraded, retry-due and stale social rows in repair order."""
        now = int(time.time()) if now is None else int(now)
        limit = max(1, min(int(limit), 200))
        ready_before = now - PREVIEW_READY_REFRESH_SECONDS
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM urls
                 WHERE disabled_at IS NULL
                   AND (lower(target_url) LIKE 'https://threads.com/%'
                        OR lower(target_url) LIKE 'https://www.threads.com/%'
                        OR lower(target_url) LIKE 'https://threads.net/%'
                        OR lower(target_url) LIKE 'https://www.threads.net/%'
                        OR lower(target_url) LIKE 'https://instagram.com/%'
                        OR lower(target_url) LIKE 'https://www.instagram.com/%')
                """,
            ).fetchall()
        candidates = []
        for raw_row in rows:
            row = dict(raw_row)
            status = row.get("preview_status") or "pending"
            cached_path, _ = _preview_image_cache_path(row.get("code") or "")
            metadata = {
                "title": row.get("preview_title") or "",
                "description": row.get("preview_description") or "",
                "image": "",
            }
            degraded = bool(
                social_preview_quality_issues(
                    row.get("target_url") or "",
                    metadata,
                    image_path=cached_path or "",
                    require_image=True,
                )
            )
            if status == "pending":
                priority = 0
            elif status in {"fallback", "profile_fallback"} and int(row.get("preview_next_retry_at") or 0) <= now:
                priority = 1
            elif status in {"ready", "profile_fallback"} and degraded:
                priority = 2
            elif status == "ready" and int(row.get("preview_updated_at") or 0) <= ready_before:
                priority = 3
            else:
                continue
            age_key = int(row.get("preview_next_retry_at") or row.get("preview_updated_at") or row.get("created_at") or 0)
            candidates.append((priority, age_key, int(row.get("created_at") or 0), row))
        candidates.sort(key=lambda item: item[:3])
        return [item[3] for item in candidates[:limit]]

    def get_alert_state(self, key: str) -> Optional[dict]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM alert_state WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None

    def mark_alert_sent(self, key: str, value: int = 0) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO alert_state(key, last_sent_at, value) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET last_sent_at = excluded.last_sent_at, value = excluded.value
                """,
                (key, now, value),
            )

    def delete_url(self, code: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM urls WHERE code = ?", (code,))
            return cur.rowcount > 0

    def record_click(self, code: str, user_agent: str = "", remote_addr: str = "") -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO clicks(code, clicked_at, user_agent, remote_addr) VALUES (?, ?, ?, ?)",
                (code, now, user_agent[:500], remote_addr[:128]),
            )
            conn.execute(
                "UPDATE urls SET clicks = clicks + 1, last_clicked_at = ? WHERE code = ?",
                (now, code),
            )


@dataclass
class ShortURLApp:
    store: ShortURLStore
    base_url: str
    admin_token: str
    extension_zip_path: str = ""
    og_image_path: str = ""
    alert_email: str = DEFAULT_ALERT_EMAIL
    alert_from: str = DEFAULT_ALERT_FROM
    sendmail_path: str = DEFAULT_SENDMAIL
    public_create_enabled: bool = True
    public_daily_ip_limit: int = PUBLIC_DAILY_IP_LIMIT
    public_rate_log: Dict[str, list] = field(default_factory=dict)
    preview_warm_enabled: bool = False
    preview_jobs: set[str] = field(default_factory=set, init=False, repr=False)
    preview_jobs_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def _rate_key(self, remote_addr: str, user_agent: str = "") -> str:
        ua_hash = hashlib.sha256((user_agent or "").encode("utf-8")).hexdigest()[:12]
        return f"{remote_addr or 'unknown'}:{ua_hash}"

    def _check_public_rate_limit(
        self,
        remote_addr: str,
        user_agent: str = "",
        max_count: int = PUBLIC_RATE_LIMIT_COUNT,
    ) -> Optional[Tuple[int, Dict[str, str], bytes]]:
        if is_private_client(remote_addr):
            return None
        now = time.time()
        key = self._rate_key(remote_addr, user_agent)
        hits = [ts for ts in self.public_rate_log.get(key, []) if now - ts < PUBLIC_RATE_LIMIT_WINDOW]
        if len(hits) >= max_count:
            self.public_rate_log[key] = hits
            return self._json(429, {"error": "短時間內使用次數過多，請稍後再試。"})
        hits.append(now)
        self.public_rate_log[key] = hits
        if self.public_daily_ip_limit > 0:
            day_start = int(time.time()) - 24 * 60 * 60
            if self.store.count_urls_by_creator_ip_since(remote_addr or "unknown", day_start) >= self.public_daily_ip_limit:
                return self._json(429, {"error": "今日建立短網址次數已達上限，請明天再試。"})
        return None

    def _check_public_create_enabled(self, is_lan: bool = False) -> Optional[Tuple[int, Dict[str, str], bytes]]:
        if self.public_create_enabled or is_lan:
            return None
        return self._json(503, {"error": "公開建立短網址功能暫時關閉，既有短網址仍可正常開啟。"})

    def _public_form_token(self) -> str:
        issued_at = str(int(time.time()))
        secret = self.admin_token.encode("utf-8")
        digest = hmac.new(secret, issued_at.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{issued_at}.{digest}"

    def _valid_public_form_token(self, token: str) -> bool:
        try:
            issued_at, digest = (token or "").split(".", 1)
            age = time.time() - int(issued_at)
        except (ValueError, TypeError):
            return False
        if age < 0 or age > PUBLIC_FORM_TOKEN_MAX_AGE:
            return False
        expected = hmac.new(self.admin_token.encode("utf-8"), issued_at.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(digest, expected)

    def _warm_preview(self, row: dict, force_image_refresh: bool = False) -> None:
        code = (row.get("code") or "").strip()
        target_url = (row.get("target_url") or "").strip()
        if not code or not is_social_preview_target(target_url):
            return
        existing_metadata = {
            "title": row.get("preview_title") or "",
            "description": row.get("preview_description") or "",
            "image": "",
        }
        existing_cached_path, _ = _preview_image_cache_path(code)
        existing_status = row.get("preview_status") or ""
        existing_is_healthy = existing_status in {"ready", "profile_fallback"} and not social_preview_quality_issues(
            target_url,
            existing_metadata,
            image_path=existing_cached_path or "",
            require_image=True,
        )

        metadata = sanitize_preview_metadata(target_url, fetch_open_graph_metadata(target_url))
        status = "ready"
        preserve_existing = False
        source_issues = social_preview_quality_issues(target_url, metadata, require_image=False)
        if source_issues:
            metadata = sanitize_preview_metadata(target_url, fetch_social_profile_fallback_metadata(target_url))
            profile_issues = social_preview_quality_issues(target_url, metadata, require_image=False)
            if profile_issues and existing_is_healthy:
                metadata = existing_metadata
                status = existing_status
                preserve_existing = True
            elif profile_issues:
                metadata = {}
                status = "fallback"
            else:
                status = "profile_fallback"
        if preserve_existing:
            cached_path = existing_cached_path
        elif force_image_refresh:
            cached_path, _ = cache_preview_image(
                code, metadata.get("image", ""), force=True
            )
        else:
            cached_path, _ = cache_preview_image(code, metadata.get("image", ""))
        final_issues = social_preview_quality_issues(
            target_url,
            metadata,
            image_path=cached_path or "",
            require_image=True,
        )
        if status in {"ready", "profile_fallback"} and final_issues:
            status = "fallback"
        self.store.update_preview_metadata(
            code,
            metadata,
            status,
            image_path=cached_path or "",
            error=("preview quality: " + ",".join(final_issues)) if status == "fallback" else "",
        )

    def _warm_preview_async(self, row: dict) -> None:
        if not self.preview_warm_enabled or not is_social_preview_target(row.get("target_url") or ""):
            return
        code = (row.get("code") or "").strip()
        if not code:
            return
        with self.preview_jobs_lock:
            if code in self.preview_jobs:
                return
            self.preview_jobs.add(code)

        def worker() -> None:
            try:
                self._warm_preview(row)
            except Exception as exc:
                print(f"preview-warm code={code} status=error error={type(exc).__name__}:{exc}", flush=True)
            finally:
                with self.preview_jobs_lock:
                    self.preview_jobs.discard(code)

        threading.Thread(target=worker, name=f"preview-warm-{code}", daemon=True).start()

    def _prepare_preview_for_share(self, row: dict, wait_for_preview: bool) -> dict:
        """Warm a new social URL before returning it, preventing first-share fallback metadata from being cached."""
        if not self.preview_warm_enabled or not is_social_preview_target(row.get("target_url") or ""):
            return row
        has_preview_text = bool((row.get("preview_title") or "").strip() or (row.get("preview_description") or "").strip())
        if wait_for_preview and not has_preview_text:
            try:
                self._warm_preview(row)
            except Exception as exc:
                code = row.get("code") or ""
                print(f"preview-warm-sync code={code} status=error error={type(exc).__name__}:{exc}", flush=True)
            return self.store.lookup(row.get("code") or "") or row
        self._warm_preview_async(row)
        return row

    def _create_and_alert(
        self,
        target_url: str,
        code: Optional[str] = None,
        title: Optional[str] = None,
        source: str = "",
        creator_ip: str = "",
        enforce_target_policy: bool = True,
        wait_for_preview: bool = False,
    ) -> dict:
        if enforce_target_policy:
            validate_target_url(target_url, base_url=self.base_url)
        canonical_url = canonicalize_target_url(target_url)
        target_hash = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
        existing = self.store.find_by_target_hash(target_hash)
        if existing and not code:
            if existing.get("disabled_at"):
                raise ValueError("此目標網址曾被停用，不能自動建立新的短碼。")
            return self._prepare_preview_for_share(existing, wait_for_preview)
        row = self.store.create_url(target_url, code=code, title=title, source=source, creator_ip=creator_ip)
        self._check_volume_alerts(row)
        self._check_abuse_alerts(row)
        return self._prepare_preview_for_share(row, wait_for_preview)

    def _check_volume_alerts(self, created_row: dict) -> None:
        total_count = self.store.count_urls()
        recent_count = self.store.count_urls_since(int(time.time()) - ALERT_RECENT_WINDOW_SECONDS)
        short_url = self._public_row(created_row)["short_url"]
        if recent_count >= ALERT_RECENT_COUNT_THRESHOLD:
            state = self.store.get_alert_state("recent_5m_100")
            last_sent_at = int(state["last_sent_at"]) if state else 0
            if int(time.time()) - last_sent_at >= ALERT_RECENT_COOLDOWN_SECONDS:
                subject = f"[u.kuies.tw 警示] 5 分鐘內新增 {recent_count} 筆短網址"
                body = (
                    "u.kuies.tw 短網址服務偵測到短時間大量新增資料。\n\n"
                    f"5 分鐘內新增筆數：{recent_count}\n"
                    f"目前總筆數：{total_count}\n"
                    f"最新短網址：{short_url}\n"
                    f"最新目標：{created_row.get('target_url')}\n\n"
                    "若非本人或擴充外掛正常使用，請檢查公開 API 是否遭濫用。"
                )
                if self._send_alert_email(subject, body):
                    self.store.mark_alert_sent("recent_5m_100", recent_count)
        if total_count >= ALERT_TOTAL_COUNT_THRESHOLD:
            state = self.store.get_alert_state("total_1000")
            last_value = int(state["value"]) if state else 0
            if last_value < ALERT_TOTAL_COUNT_THRESHOLD:
                subject = f"[u.kuies.tw 警示] 短網址總筆數已達 {total_count} 筆"
                body = (
                    "u.kuies.tw 短網址服務資料量已達警示門檻。\n\n"
                    f"目前總筆數：{total_count}\n"
                    f"5 分鐘內新增筆數：{recent_count}\n"
                    f"最新短網址：{short_url}\n"
                    f"最新目標：{created_row.get('target_url')}\n"
                )
                if self._send_alert_email(subject, body):
                    self.store.mark_alert_sent("total_1000", total_count)

    def _check_abuse_alerts(self, created_row: dict) -> None:
        now = int(time.time())
        since = now - ABUSE_ALERT_WINDOW_SECONDS
        creator_ip = (created_row.get("creator_ip") or "").strip()
        domain = target_domain(created_row.get("target_url") or "")
        short_url = self._public_row(created_row)["short_url"]
        if creator_ip and creator_ip not in {"lan", "unknown"}:
            count = self.store.count_urls_by_creator_ip_since(creator_ip, since)
            self._maybe_send_abuse_alert(
                f"ip_abuse:{creator_ip}",
                count,
                f"[u.kuies.tw 警示] 單一 IP 10 分鐘內新增 {count} 筆短網址",
                f"單一 IP 建立短網址數量異常。\n\nIP：{creator_ip}\n10 分鐘內新增：{count}\n最新短網址：{short_url}\n最新目標：{created_row.get('target_url')}\n",
            )
        if domain:
            count = self.store.count_urls_by_domain_since(domain, since)
            self._maybe_send_abuse_alert(
                f"domain_abuse:{domain}",
                count,
                f"[u.kuies.tw 警示] 單一網域 10 分鐘內新增 {count} 筆短網址",
                f"單一目標網域建立短網址數量異常。\n\n網域：{domain}\n10 分鐘內新增：{count}\n最新短網址：{short_url}\n最新目標：{created_row.get('target_url')}\n",
            )

    def _maybe_send_abuse_alert(self, key: str, count: int, subject: str, body: str) -> None:
        if count < ABUSE_ALERT_COUNT_THRESHOLD:
            return
        state = self.store.get_alert_state(key)
        last_sent_at = int(state["last_sent_at"]) if state else 0
        if int(time.time()) - last_sent_at < ABUSE_ALERT_COOLDOWN_SECONDS:
            return
        if self._send_alert_email(subject, body):
            self.store.mark_alert_sent(key, count)

    def _send_alert_email(self, subject: str, body: str) -> bool:
        if not self.alert_email:
            print(f"short-url alert skipped: alert email not configured: {subject}", flush=True)
            return False
        msg = EmailMessage()
        msg["To"] = self.alert_email
        msg["From"] = self.alert_from
        msg["Subject"] = subject
        msg.set_content(body)
        try:
            envelope_from = parseaddr(self.alert_from)[1] or self.alert_from
            subprocess.run([self.sendmail_path, "-f", envelope_from, "-t"], input=msg.as_bytes(), check=True, timeout=15)
            print(f"short-url alert sent to {self.alert_email}: {subject}", flush=True)
            return True
        except Exception as exc:
            print(f"short-url alert send failed: {exc}: {subject}", flush=True)
            return False

    def handle(
        self,
        method: str,
        path: str,
        headers: Dict[str, str],
        body: bytes,
        remote_addr: str,
        user_agent: str,
    ) -> Tuple[int, Dict[str, str], bytes]:
        clean_path = urlparse(path).path
        is_lan = is_private_client(remote_addr)
        if clean_path in {"/", ""} and method in {"GET", "HEAD"}:
            return self._html_home(is_lan=is_lan)
        if clean_path == "/privacy/threads-link-cleaner" and method in {"GET", "HEAD"}:
            return self._html_extension_privacy()
        if clean_path == "/report" and method in {"GET", "HEAD"}:
            return self._html_report()
        if clean_path == "/surl" and method in {"GET", "HEAD"}:
            return self._html_public(is_lan=is_lan)
        if clean_path == "/gkd" and method in {"GET", "HEAD"}:
            guide_path = os.path.join(os.path.dirname(__file__), "static", "guides", "gkd-icashpay.html")
            return self._download_public_file(guide_path, "text/html; charset=utf-8")
        if clean_path == "/ucl" and method in {"GET", "HEAD"}:
            catalog_path = os.path.join(os.path.dirname(__file__), "static", "urlcheck", "social-rules.json")
            return self._download_public_file(catalog_path, "application/json; charset=utf-8")
        if clean_path == "/uch" and method in {"GET", "HEAD"}:
            hash_path = os.path.join(os.path.dirname(__file__), "static", "urlcheck", "social-rules.sha256")
            return self._download_public_file(hash_path, "text/plain; charset=utf-8")
        if clean_path == "/ucp" and method in {"GET", "HEAD"}:
            patterns_path = os.path.join(os.path.dirname(__file__), "static", "urlcheck", "social-resolver-patterns.json")
            return self._download_public_file(patterns_path, "application/json; charset=utf-8")
        if clean_path == "/sr" and method in {"GET", "HEAD"}:
            raw_query = parse_qs(urlparse(path).query, keep_blank_values=True)
            source = (raw_query.get("url") or [""])[0].strip()
            if len(source) > 2048 or not is_supported_social_share_wrapper(source):
                return self._json(400, {"error": "unsupported social share wrapper"})
            resolved = prepare_target_url(source, clean_tracking=True)
            if resolved == source or is_supported_social_share_wrapper(resolved):
                return self._json(422, {"error": "unable to resolve social share wrapper"})
            return 302, {
                "Location": resolved,
                "Cache-Control": "private, no-store",
                "X-Robots-Tag": "noindex, nofollow",
            }, b""
        if clean_path == "/downloads/threads-link-cleaner.zip" and method in {"GET", "HEAD"}:
            return self._download_extension_zip()
        if clean_path == "/downloads/kuies-private-dns-dot.mobileconfig" and method in {"GET", "HEAD"}:
            return self._download_public_file(
                "./static/downloads/kuies-private-dns-dot.mobileconfig",
                "application/x-apple-aspen-config",
                'attachment; filename="kuies-private-dns-dot.mobileconfig"',
            )
        if clean_path == "/og-image.png" and method in {"GET", "HEAD"}:
            return self._download_og_image()
        if clean_path == "/meme/vitamin-c-cat.jpg" and method in {"GET", "HEAD"}:
            return self._download_public_image("./data/meme_vitamin_c_cat.jpg", "image/jpeg")
        if clean_path.startswith("/preview-image/") and method in {"GET", "HEAD"}:
            encoded_filename = clean_path[len("/preview-image/"):]
            decoded_filename = unquote(encoded_filename)
            code = "" if "/" in decoded_filename else self._preview_code_from_filename(decoded_filename)
            if code:
                return self._download_preview_image(code)
        if clean_path.startswith("/go/") and method in {"GET", "HEAD"}:
            code = unquote(clean_path.rsplit("/", 1)[-1])
            if SAFE_CODE_RE.match(code):
                return self._confirmed_redirect(code, method, user_agent, remote_addr)
        if clean_path == "/shorten" and method == "POST":
            return self._public_create(body, is_lan=is_lan, remote_addr=remote_addr, user_agent=user_agent)
        if clean_path == "/surl/manage" and method == "POST":
            return self._admin_manage(body, is_lan=is_lan)
        if clean_path == "/surl/urls" and method == "POST":
            return self._admin_create(body, is_lan=is_lan)
        if clean_path == "/surl/delete" and method == "POST":
            return self._admin_delete(body, is_lan=is_lan)
        if clean_path == "/surl/disable" and method == "POST":
            return self._admin_disable(body, is_lan=is_lan)
        if clean_path == "/healthz" and method in {"GET", "HEAD"}:
            return self._json(200, {"ok": True, "service": "kuies-short-url"})
        if clean_path == "/api/public/shorten" and method == "OPTIONS":
            return self._cors_json(204, {})
        if clean_path == "/api/public/shorten" and method == "POST":
            return self._public_create_json(body, remote_addr=remote_addr, user_agent=user_agent)
        if clean_path.startswith("/api/public/preview-status/") and method in {"GET", "HEAD"}:
            code = unquote(clean_path.rsplit("/", 1)[-1])
            if not SAFE_CODE_RE.fullmatch(code or ""):
                return self._cors_json(404, {"error": "not_found"})
            row = self.store.lookup(code)
            if not row or row.get("disabled_at"):
                return self._cors_json(404, {"error": "not_found"})
            return self._cors_json(200, self._preview_status_row(row))
        if clean_path == "/api/urls" and method == "POST":
            return self._create(headers, body)
        if clean_path == "/api/urls" and method == "GET":
            if not self._authorized(headers):
                return self._json(401, {"error": "unauthorized"})
            return self._json(200, {"urls": [self._public_row(row) for row in self.store.list_urls()]})
        if clean_path.startswith("/api/urls/") and method == "DELETE":
            if not self._authorized(headers):
                return self._json(401, {"error": "unauthorized"})
            code = unquote(clean_path.rsplit("/", 1)[-1])
            return self._json(200 if self.store.delete_url(code) else 404, {"deleted": code})
        if method in {"GET", "HEAD"}:
            code = unquote(clean_path.lstrip("/"))
            if SAFE_CODE_RE.match(code):
                row = self.store.lookup(code)
                if row:
                    if row.get("disabled_at"):
                        return self._json(410, {"error": "short_url_disabled", "code": code})
                    if method == "GET" and is_preview_crawler(user_agent) and is_safe_preview_target(row["target_url"], base_url=self.base_url):
                        return self._html_preview(code, row)
                    if method == "GET" and not is_preview_crawler(user_agent) and needs_warning_page(row["target_url"]):
                        return self._html_warning(code, row)
                    if method == "GET" and not is_preview_crawler(user_agent):
                        self.store.record_click(code, user_agent=user_agent, remote_addr=remote_addr)
                    return 302, {"Location": row["target_url"], "Cache-Control": "no-store"}, b""
        return self._json(404, {"error": "not_found"})

    def _authorized(self, headers: Dict[str, str]) -> bool:
        expected = f"Bearer {self.admin_token}"
        return bool(self.admin_token) and headers.get("authorization", "") == expected

    def _create(self, headers: Dict[str, str], body: bytes) -> Tuple[int, Dict[str, str], bytes]:
        if not self._authorized(headers):
            return self._json(401, {"error": "unauthorized"})
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
            row = self._create_and_alert(payload.get("url", ""), code=payload.get("code"), title=payload.get("title"))
            return self._json(201, self._public_row(row))
        except (json.JSONDecodeError, ValueError) as exc:
            return self._json(400, {"error": str(exc)})

    def _parse_form(self, body: bytes) -> dict:
        raw = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        return {key: values[0] if values else "" for key, values in raw.items()}

    def _form_authorized(self, form: dict) -> bool:
        return bool(self.admin_token) and secrets.compare_digest(form.get("token", ""), self.admin_token)

    def _public_create(self, body: bytes, is_lan: bool, remote_addr: str = "", user_agent: str = "") -> Tuple[int, Dict[str, str], bytes]:
        disabled = self._check_public_create_enabled(is_lan=is_lan)
        if disabled:
            status, _, payload = disabled
            try:
                message = json.loads(payload.decode("utf-8")).get("error", "公開建立短網址功能暫時關閉。")
            except Exception:
                message = "公開建立短網址功能暫時關閉。"
            return self._html_public(is_lan=is_lan, message=message, status=status)
        limited = self._check_public_rate_limit(remote_addr, user_agent)
        if limited:
            status, _, payload = limited
            try:
                message = json.loads(payload.decode("utf-8")).get("error", "短時間內使用次數過多，請稍後再試。")
            except Exception:
                message = "短時間內使用次數過多，請稍後再試。"
            return self._html_public(is_lan=is_lan, message=message, status=status)
        form = self._parse_form(body)
        if form.get("website", ""):
            return self._html_public(is_lan=is_lan, message="送出失敗，請稍後再試。", status=400)
        if not is_lan and not self._valid_public_form_token(form.get("anti_bot_token", "")):
            return self._html_public(is_lan=is_lan, message="頁面驗證已過期，請重新整理後再試。", status=400)
        try:
            target_url = prepare_target_url(
                form.get("url", ""),
                clean_threads_xmt=form.get("clean_threads_xmt") == "1",
                clean_tracking=form.get("clean_tracking") == "1",
            )
            row = self._create_and_alert(
                target_url,
                code=form.get("code") or None,
                title=form.get("title") or None,
                source="web",
                creator_ip=remote_addr or "unknown",
                wait_for_preview=True,
            )
            short_url = self._public_row(row)["short_url"]
            return self._html_public(is_lan=is_lan, result_url=short_url)
        except ValueError as exc:
            return self._html_public(is_lan=is_lan, message=str(exc), status=400)

    def _public_create_json(self, body: bytes, remote_addr: str = "", user_agent: str = "") -> Tuple[int, Dict[str, str], bytes]:
        disabled = self._check_public_create_enabled(is_lan=is_private_client(remote_addr))
        if disabled:
            status, _, payload = disabled
            try:
                return self._cors_json(status, json.loads(payload.decode("utf-8")))
            except Exception:
                return self._cors_json(status, {"error": "公開建立短網址功能暫時關閉。"})
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            return self._cors_json(400, {"error": str(exc)})
        fast_response = payload.get("fast_response") is True
        rate_limit = PUBLIC_EXTENSION_RATE_LIMIT_COUNT if fast_response else PUBLIC_RATE_LIMIT_COUNT
        limited = self._check_public_rate_limit(remote_addr, user_agent, max_count=rate_limit)
        if limited:
            status, _, limited_payload = limited
            try:
                return self._cors_json(status, json.loads(limited_payload.decode("utf-8")))
            except Exception:
                return self._cors_json(status, {"error": "短時間內使用次數過多，請稍後再試。"})
        try:
            target_url = prepare_target_url(
                payload.get("url", ""),
                clean_threads_xmt=bool(payload.get("clean_threads_xmt")),
                clean_tracking=bool(payload.get("clean_tracking")),
            )
            row = self._create_and_alert(
                target_url,
                code=payload.get("code") or None,
                title=payload.get("title") or None,
                source="extension",
                creator_ip=remote_addr or "unknown",
                wait_for_preview=not fast_response,
            )
            return self._cors_json(201, self._public_row(row))
        except ValueError as exc:
            return self._cors_json(400, {"error": str(exc)})

    def _require_lan(self, is_lan: bool) -> Optional[Tuple[int, Dict[str, str], bytes]]:
        if is_lan:
            return None
        return self._html_public(is_lan=False, message="管理功能僅限內網使用。", status=403)

    def _admin_manage(self, body: bytes, is_lan: bool) -> Tuple[int, Dict[str, str], bytes]:
        blocked = self._require_lan(is_lan)
        if blocked:
            return blocked
        form = self._parse_form(body)
        if not self._form_authorized(form):
            return self._html_public(is_lan=True, message="管理金鑰錯誤。", status=401)
        return self._html_manage(token=form.get("token", ""))

    def _admin_create(self, body: bytes, is_lan: bool) -> Tuple[int, Dict[str, str], bytes]:
        blocked = self._require_lan(is_lan)
        if blocked:
            return blocked
        form = self._parse_form(body)
        if not self._form_authorized(form):
            return self._html_public(is_lan=True, message="管理金鑰錯誤，沒有建立短網址。", status=401)
        try:
            target_url = prepare_target_url(
                form.get("url", ""),
                clean_threads_xmt=form.get("clean_threads_xmt") == "1",
                clean_tracking=form.get("clean_tracking") == "1",
            )
            row = self._create_and_alert(
                target_url,
                code=form.get("code") or None,
                title=form.get("title") or None,
                source="admin",
                creator_ip="lan",
                enforce_target_policy=False,
            )
            return self._html_manage(message=f"已建立：{self._public_row(row)['short_url']}", token=form.get("token", ""))
        except ValueError as exc:
            return self._html_manage(message=str(exc), status=400, token=form.get("token", ""))

    def _admin_delete(self, body: bytes, is_lan: bool) -> Tuple[int, Dict[str, str], bytes]:
        blocked = self._require_lan(is_lan)
        if blocked:
            return blocked
        form = self._parse_form(body)
        if not self._form_authorized(form):
            return self._html_public(is_lan=True, message="管理金鑰錯誤，沒有刪除短網址。", status=401)
        code = form.get("code", "").strip()
        if not SAFE_CODE_RE.match(code):
            return self._html_manage(message="短碼格式不正確。", status=400, token=form.get("token", ""))
        deleted = self.store.delete_url(code)
        message = f"已刪除：{code}" if deleted else f"找不到短碼：{code}"
        return self._html_manage(message=message, status=200 if deleted else 404, token=form.get("token", ""))

    def _admin_disable(self, body: bytes, is_lan: bool) -> Tuple[int, Dict[str, str], bytes]:
        blocked = self._require_lan(is_lan)
        if blocked:
            return blocked
        form = self._parse_form(body)
        if not self._form_authorized(form):
            return self._html_public(is_lan=True, message="管理金鑰錯誤，沒有更新短網址。", status=401)
        code = form.get("code", "").strip()
        if not SAFE_CODE_RE.match(code):
            return self._html_manage(message="短碼格式不正確。", status=400, token=form.get("token", ""))
        disabled = form.get("disabled", "1") == "1"
        ok = self.store.set_disabled(code, disabled=disabled, reason=form.get("reason", "管理員手動停用"))
        action = "停用" if disabled else "啟用"
        message = f"已{action}：{code}" if ok else f"找不到短碼：{code}"
        return self._html_manage(message=message, status=200 if ok else 404, token=form.get("token", ""))

    def _preview_code_from_filename(self, filename: str) -> str:
        match = re.fullmatch(r"([A-Za-z0-9_-]{1,90})\.(jpg|jpeg|png|webp)", filename or "", re.I)
        if not match:
            return ""
        stem = match.group(1)
        if len(stem) <= 64 and SAFE_CODE_RE.fullmatch(stem) and self.store.lookup(stem):
            return stem
        versioned = re.fullmatch(r"([A-Za-z0-9_-]{1,64})-([0-9a-f]{12})", stem, re.I)
        if not versioned:
            return ""
        code = versioned.group(1)
        return code if self.store.lookup(code) else ""

    def _preview_image_url(
        self,
        row: dict,
        cached_path: str = "",
        metadata: Optional[dict] = None,
    ) -> str:
        code = row.get("code") or ""
        if not cached_path:
            found_path, _ = _preview_image_cache_path(code)
            cached_path = found_path or ""
        ext = os.path.splitext(cached_path)[1].lower() if cached_path else ".png"
        if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
            ext = ".jpg"
        version_metadata = metadata or {
                "title": row.get("preview_title") or "",
                "description": row.get("preview_description") or "",
                "image": "",
            }
        version = row.get("preview_version") or preview_content_version(
            version_metadata,
            image_path=cached_path or "",
        )
        return f"{self.base_url.rstrip('/')}/preview-image/{quote(code)}-{version}{ext}"

    def _preview_status_row(self, row: dict) -> dict:
        status = row.get("preview_status") or "pending"
        cached_path = None
        if status != "pending":
            cached_path, _ = _preview_image_cache_path(row.get("code") or "")
        available = bool(cached_path and os.path.isfile(cached_path)) or bool(
            status != "pending" and self.og_image_path and os.path.isfile(self.og_image_path)
        )
        return {
            "code": row.get("code"),
            "preview_status": status,
            "preview_available": available,
            "preview_image_url": self._preview_image_url(row) if available else None,
            "preview_updated_at": row.get("preview_updated_at"),
            "preview_next_retry_at": row.get("preview_next_retry_at"),
        }

    def _public_row(self, row: dict) -> dict:
        public = {
            "code": row["code"],
            "target_url": row["target_url"],
            "title": row.get("title"),
            "short_url": f"{self.base_url.rstrip('/')}/{quote(row['code'])}",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "clicks": row["clicks"],
            "last_clicked_at": row.get("last_clicked_at"),
            "source": row.get("source"),
            "creator_ip": row.get("creator_ip"),
            "disabled_at": row.get("disabled_at"),
            "disabled_reason": row.get("disabled_reason"),
            "canonical_url": row.get("canonical_url"),
            "target_hash": row.get("target_hash"),
            "preview_title": row.get("preview_title"),
            "preview_description": row.get("preview_description"),
            "preview_status": row.get("preview_status") or "pending",
            "preview_updated_at": row.get("preview_updated_at"),
        }
        public.update(self._preview_status_row(row))
        return public

    def _download_extension_zip(self) -> Tuple[int, Dict[str, str], bytes]:
        if not self.extension_zip_path or not os.path.exists(self.extension_zip_path):
            return self._json(404, {"error": "extension_not_found"})
        with open(self.extension_zip_path, "rb") as fh:
            data = fh.read()
        return 200, {
            "Content-Type": "application/zip",
            "Content-Disposition": 'attachment; filename="threads-link-cleaner.zip"',
        }, data

    def _download_og_image(self) -> Tuple[int, Dict[str, str], bytes]:
        if not self.og_image_path or not os.path.exists(self.og_image_path):
            return self._json(404, {"error": "og_image_not_found"})
        with open(self.og_image_path, "rb") as fh:
            data = fh.read()
        return 200, {
            "Content-Type": "image/png",
            "Cache-Control": "public, max-age=86400",
        }, data

    def _download_public_file(self, file_path: str, content_type: str, content_disposition: str = "") -> Tuple[int, Dict[str, str], bytes]:
        if not file_path or not os.path.exists(file_path):
            return self._json(404, {"error": "file_not_found"})
        with open(file_path, "rb") as fh:
            data = fh.read()
        headers = {
            "Content-Type": content_type,
            "Cache-Control": "public, max-age=3600",
        }
        if content_disposition:
            headers["Content-Disposition"] = content_disposition
        return 200, headers, data

    def _download_public_image(self, image_path: str, content_type: str) -> Tuple[int, Dict[str, str], bytes]:
        if not image_path or not os.path.exists(image_path):
            return self._json(404, {"error": "image_not_found"})
        with open(image_path, "rb") as fh:
            data = fh.read()
        return 200, {
            "Content-Type": content_type,
            "Cache-Control": "public, max-age=86400",
        }, data

    def _download_preview_image(self, code: str) -> Tuple[int, Dict[str, str], bytes]:
        row = self.store.lookup(code)
        if not row or not is_safe_preview_target(row["target_url"], base_url=self.base_url):
            return self._json(404, {"error": "preview_image_not_found"})
        cached_path, content_type = _preview_image_cache_path(code)
        if not cached_path:
            if self.preview_warm_enabled and is_social_preview_target(row["target_url"]):
                self._warm_preview_async(row)
            else:
                metadata = sanitize_preview_metadata(row["target_url"], fetch_open_graph_metadata(row["target_url"]))
                cached_path, content_type = cache_preview_image(code, metadata.get("image", ""))
        if not cached_path or not os.path.exists(cached_path):
            return self._download_og_image()
        with open(cached_path, "rb") as fh:
            data = fh.read()
        return 200, {
            "Content-Type": content_type or "image/jpeg",
            "Cache-Control": "public, max-age=604800, immutable",
            "X-Robots-Tag": "noindex",
        }, data

    def _html_preview(self, code: str, row: dict) -> Tuple[int, Dict[str, str], bytes]:
        target_url = row["target_url"]
        short_url = f"{self.base_url.rstrip('/')}/{quote(code)}"
        host_label = display_host(target_url)
        if self.preview_warm_enabled and is_social_preview_target(target_url):
            metadata = {
                "title": row.get("preview_title") or "",
                "description": row.get("preview_description") or "",
                "image": "",
            }
            cached_path, cached_type = _preview_image_cache_path(code)
            updated_at = int(row.get("preview_updated_at") or 0)
            now = int(time.time())
            due = (
                not updated_at
                or (row.get("preview_status") == "ready" and now - updated_at >= PREVIEW_READY_REFRESH_SECONDS)
                or (
                    row.get("preview_status") in {"fallback", "profile_fallback", "pending", None, ""}
                    and int(row.get("preview_next_retry_at") or 0) <= now
                )
            )
            if due:
                self._warm_preview_async(row)
        else:
            metadata = sanitize_preview_metadata(target_url, fetch_open_graph_metadata(target_url))
            cached_path, cached_type = cache_preview_image(code, (metadata.get("image") or "").strip())
        chosen_title, chosen_description = choose_preview_text(
            host_label, metadata, row.get("title") or "", target_url=target_url
        )
        title = (chosen_title or f"開啟 {host_label} 連結").strip()
        description = (chosen_description or f"透過 kuies.tw 短網址前往 {host_label}。").strip()
        if cached_path:
            image_url = self._preview_image_url(row, cached_path=cached_path, metadata=metadata)
            image_type = cached_type or "image/jpeg"
        else:
            # Give every short code a stable first-party image URL even when the
            # source is private, deleted, rate-limited or serving a login wall.
            # Social platforms cache images aggressively; a shared fallback URL
            # lets one failed fetch poison previews for unrelated links.
            image_url = self._preview_image_url(row, metadata=metadata)
            image_type = "image/png"
        safe_title = html.escape(title, quote=True)
        safe_description = html.escape(description, quote=True)
        safe_short = html.escape(short_url, quote=True)
        safe_target = html.escape(target_url, quote=True)
        safe_image = html.escape(image_url, quote=True)
        safe_site = html.escape(host_label, quote=True)
        html_doc = f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title}</title>
<meta name="description" content="{safe_description}">
<link rel="canonical" href="{safe_target}">
<meta property="og:locale" content="zh_TW">
<meta property="og:type" content="article">
<meta property="og:site_name" content="{safe_site}">
<meta property="og:title" content="{safe_title}">
<meta property="og:description" content="{safe_description}">
<meta property="og:url" content="{safe_short}">
<meta property="og:image" content="{safe_image}">
<meta property="og:image:secure_url" content="{safe_image}">
<meta property="og:image:type" content="{html.escape(image_type, quote=True)}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="article:author" content="{safe_site}">
<meta property="article:published_time" content="{time.strftime('%Y-%m-%dT%H:%M:%S+08:00', time.localtime(row.get('created_at') or time.time()))}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:site" content="{safe_site}">
<meta name="twitter:title" content="{safe_title}">
<meta name="twitter:description" content="{safe_description}">
<meta name="twitter:image" content="{safe_image}">
<meta name="twitter:image:alt" content="{safe_description}">
<meta http-equiv="refresh" content="0;url={safe_target}">
</head>
<body>
<article>
<h1>{safe_title}</h1>
<p>{safe_description}</p>
<p><img src="{safe_image}" alt="{safe_description}" style="max-width:100%;height:auto"></p>
<p><a href="{safe_target}">前往原始連結</a></p>
</article>
</body>
</html>"""
        return 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}, html_doc.encode("utf-8")

    def _confirmed_redirect(self, code: str, method: str, user_agent: str = "", remote_addr: str = "") -> Tuple[int, Dict[str, str], bytes]:
        row = self.store.lookup(code)
        if not row:
            return self._json(404, {"error": "not_found"})
        if row.get("disabled_at"):
            return self._json(410, {"error": "short_url_disabled", "code": code})
        if method == "GET":
            self.store.record_click(code, user_agent=user_agent, remote_addr=remote_addr)
        return 302, {"Location": row["target_url"], "Cache-Control": "no-store", "X-Robots-Tag": "noindex"}, b""

    def _html_warning(self, code: str, row: dict) -> Tuple[int, Dict[str, str], bytes]:
        target_url = row["target_url"]
        domain = target_domain(target_url) or "未知網域"
        keywords = ", ".join(risk_keywords_for_url(target_url)) or "高風險字詞"
        safe_domain = html.escape(domain, quote=True)
        safe_target = html.escape(target_url, quote=True)
        safe_keywords = html.escape(keywords, quote=True)
        go_url = f"/go/{quote(code)}"
        html_doc = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>高風險連結提醒</title>{self._style()}</head>
<body><h1>⚠️ 高風險連結提醒</h1><div class="card">
<p class="warn">這個短網址的目標包含常見釣魚或登入驗證相關字詞：{safe_keywords}</p>
<p>目標網域：<strong>{safe_domain}</strong></p>
<p class="target">完整網址：{safe_target}</p>
<p>如果你不認識這個網站，請不要輸入密碼、驗證碼、信用卡、錢包助記詞或任何帳號資料。</p>
<p><a class="button" href="{go_url}" rel="nofollow noopener noreferrer">我了解風險，繼續前往</a></p>
<p><a href="/report">檢舉可疑短網址</a></p>
</div></body></html>"""
        return 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "X-Robots-Tag": "noindex,nofollow"}, html_doc.encode("utf-8")

    def _html_report(self) -> Tuple[int, Dict[str, str], bytes]:
        mailto = "mailto:short-url@example.com?subject=%E6%AA%A2%E8%88%89%20u.kuies.tw%20%E7%9F%AD%E7%B6%B2%E5%9D%80"
        html_doc = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>檢舉 u.kuies.tw 短網址</title>{self._style()}</head>
<body><h1>🚩 檢舉 u.kuies.tw 短網址</h1><div class="card">
<p>如果你認為某個 <code>u.kuies.tw</code> 短網址涉及詐騙、惡意軟體、釣魚、冒充或其他濫用，請寄信回報。</p>
<p>請提供：</p><ol><li>可疑短網址</li><li>你遇到的問題</li><li>相關截圖或說明</li></ol>
<p><a href="{mailto}">寄信到 short-url@example.com</a></p>
</div></body></html>"""
        return 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "X-Robots-Tag": "noindex,nofollow"}, html_doc.encode("utf-8")

    def _json(self, status: int, payload: dict) -> Tuple[int, Dict[str, str], bytes]:
        return status, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _cors_json(self, status: int, payload: dict) -> Tuple[int, Dict[str, str], bytes]:
        out_status, headers, data = self._json(status, payload)
        headers.update({
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Cache-Control": "no-store",
        })
        return out_status, headers, data

    def _style(self) -> str:
        return """
<style>
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:960px;margin:36px auto;padding:0 18px;line-height:1.65;background:#0f172a;color:#e2e8f0}
h1{margin-bottom:8px} a{color:#7dd3fc} input,button{font-size:16px} input{width:100%;box-sizing:border-box;margin:6px 0 14px;padding:11px;border-radius:10px;border:1px solid #334155;background:#111827;color:#e2e8f0}
button{padding:10px 14px;border:0;border-radius:10px;background:#38bdf8;color:#082f49;font-weight:700;cursor:pointer}.danger{background:#fb7185;color:#450a0a;margin-top:8px}.ghost{background:#334155;color:#e2e8f0}.bot-field{position:absolute;left:-10000px;top:auto;width:1px;height:1px;overflow:hidden}
.card{background:#111827;border:1px solid #334155;border-radius:18px;padding:20px;margin:18px 0}.muted,.target,.meta{color:#94a3b8}.target{word-break:break-all}.notice{background:#0f766e;color:#ecfeff;padding:10px 12px;border-radius:12px}.warn{background:#7f1d1d;color:#fee2e2;padding:10px 12px;border-radius:12px}.result{font-size:20px;word-break:break-all}
.item{list-style:none;border-top:1px solid #334155;padding:14px 0} ul{padding:0}.grid{display:grid;grid-template-columns:1fr 180px;gap:12px}@media(max-width:720px){.grid{grid-template-columns:1fr}}
</style>"""

    def _html_home(self, is_lan: bool) -> Tuple[int, Dict[str, str], bytes]:
        manage = "<p>短網址頁面：<a href='/surl'>/surl</a></p>"
        html_doc = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>kuies.tw 短網址</title>{self._style()}</head>
<body><h1>🔗 kuies.tw 短網址</h1><div class="card">
{manage}
<p>健康檢查：<a href="/healthz">/healthz</a></p>
<p>Chrome 擴充隱私權政策：<a href="/privacy/threads-link-cleaner">/privacy/threads-link-cleaner</a></p>
<p class="muted">公開頁面可縮網址；管理清單只在內網且輸入管理金鑰後顯示。</p>
</div></body></html>"""
        return 200, {"Content-Type": "text/html; charset=utf-8"}, html_doc.encode("utf-8")

    def _html_extension_privacy(self) -> Tuple[int, Dict[str, str], bytes]:
        html_doc = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>kuies.tw Short URL 隱私權政策</title>{self._style()}</head>
<body>
<h1>🔐 kuies.tw Short URL 隱私權政策</h1>
<div class="card">
<p>最後更新：2026-06-15</p>
<p>kuies.tw Short URL Chrome 擴充的單一用途，是在使用者主動縮短網址，或在 Threads／Instagram 複製分享連結時，移除常見追蹤參數並建立 u.kuies.tw 短網址。</p>
<h2>收集與傳輸的資料</h2>
<p>擴充只會在使用者手動點擊縮短功能，或啟用自動模式並在支援網站複製連結時，將該網址傳送到 <code>https://u.kuies.tw/api/public/shorten</code> 以建立短網址。服務端會儲存原始網址、短碼、建立時間、更新時間、點擊次數與基本伺服器請求紀錄，以提供短網址跳轉、濫用防護與維運除錯。</p>
<h2>剪貼簿與網站內容</h2>
<p>擴充使用剪貼簿權限是為了讀取使用者要求縮短的剪貼簿網址，並將建立好的短網址寫回剪貼簿。擴充不會讀取或上傳非網址內容；自動模式僅在 Threads／Instagram 頁面偵測到分享網址時處理該網址。</p>
<h2>資料使用與分享</h2>
<p>資料僅用於提供短網址、追蹤參數清理、點擊跳轉、防濫用與服務維運。不出售資料，不用於廣告投放，不與第三方分享，除非法律要求或維護服務安全所必須。</p>
<h2>遠端程式碼</h2>
<p>擴充不執行遠端程式碼；所有擴充程式碼均包含在安裝套件中。擴充會呼叫 u.kuies.tw API 以建立短網址。</p>
<h2>資料刪除</h2>
<p>若需要刪除由此服務建立的短網址資料，請聯絡服務維護者處理。</p>
</div>
</body></html>"""
        return 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "public, max-age=3600"}, html_doc.encode("utf-8")

    def _shorten_form(self) -> str:
        token = html.escape(self._public_form_token())
        return f"""
<div class="card">
  <h2>縮短網址</h2>
  <form method="post" action="/shorten">
    <input name="anti_bot_token" type="hidden" value="{token}">
    <label class="bot-field">網站<input name="website" tabindex="-1" autocomplete="off"></label>
    <label>長網址</label>
    <input name="url" type="url" placeholder="https://example.com/very/long/url" required>
    <div class="grid">
      <div><label>短碼，建議 ≤5 字元，可留空自動產生</label><input name="code" maxlength="64" pattern="[A-Za-z0-9_-]+" placeholder="例如 pay 或 a7k2x"></div>
      <div><label>標題，可選</label><input name="title" placeholder="備註名稱"></div>
    </div>
    <label><input name="clean_tracking" type="checkbox" value="1" style="width:auto;margin-right:8px">移除追蹤參數</label>
    <button type="submit">縮短網址</button>
    <p class="muted">為避免被濫用或攻擊，公開頁面有基本反爬蟲檢查，且短時間內不可多次建立短網址。</p>
  </form>
</div>
<div class="card">
  <h2>Chrome 擴充</h2>
  <p><a href="/downloads/threads-link-cleaner.zip">點此下載安裝 Chrome 擴充</a></p>
  <p class="muted">下載後先解壓縮，再到 Chrome 擴充功能頁開啟「開發人員模式」→「載入未封裝項目」。追蹤清理目前支援 Threads、Facebook 與 IG。</p>
</div>"""

    def _html_public(self, is_lan: bool, result_url: str = "", message: str = "", status: int = 200) -> Tuple[int, Dict[str, str], bytes]:
        msg_class = "warn" if status >= 400 else "notice"
        message_html = f"<p class='{msg_class}'>{html.escape(message)}</p>" if message else ""
        result_html = ""
        if result_url:
            safe_result = html.escape(result_url)
            result_html = f"""
<div class="card">
  <h2>已成功縮短</h2>
  <p class="result"><a id="shortUrl" href="{safe_result}" target="_blank" rel="noopener">{safe_result}</a></p>
  <button type="button" onclick="navigator.clipboard.writeText(document.getElementById('shortUrl').textContent).then(()=>this.textContent='已複製')">複製網址</button>
  <p class="muted">這個提示只在本次送出後顯示；關閉或重新整理頁面就不會保留。</p>
</div>"""
        manage_html = ""
        if is_lan:
            manage_html = """
<div class="card">
  <h2>管理欄位</h2>
  <form method="post" action="/surl/manage">
    <label>管理金鑰</label>
    <input name="token" type="password" autocomplete="current-password" required>
    <button type="submit">進入管理介面</button>
  </form>
</div>"""
        html_doc = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>kuies.tw 短網址</title>{self._style()}</head>
<body>
<h1>🔗 kuies.tw 短網址</h1>
{message_html}
{result_html}
{self._shorten_form()}
{manage_html}
</body></html>"""
        return status, {"Content-Type": "text/html; charset=utf-8"}, html_doc.encode("utf-8")

    def _html_manage(self, message: str = "", status: int = 200, token: str = "") -> Tuple[int, Dict[str, str], bytes]:
        rows = []
        for row in self.store.list_urls(limit=200):
            public = self._public_row(row)
            code = html.escape(public["code"])
            short_url = html.escape(public["short_url"])
            target_url = html.escape(public["target_url"])
            title = html.escape(public.get("title") or "")
            clicks = html.escape(str(public["clicks"]))
            source = html.escape(public.get("source") or "未知")
            creator_ip = html.escape(public.get("creator_ip") or "")
            disabled = bool(public.get("disabled_at"))
            status_text = "已停用" if disabled else "啟用中"
            toggle_text = "啟用" if disabled else "停用"
            toggle_value = "0" if disabled else "1"
            disabled_reason = html.escape(public.get("disabled_reason") or "")
            rows.append(f"""
            <li class="item">
              <div><a href="{short_url}" target="_blank" rel="noopener">{short_url}</a></div>
              <div class="target">{target_url}</div>
              <div class="meta">標題：{title or '未命名'}　點擊：{clicks}　來源：{source}　建立 IP：{creator_ip}　狀態：{status_text}{('　原因：' + disabled_reason) if disabled_reason else ''}</div>
              <form method="post" action="/surl/disable" style="display:inline-block;margin-right:8px">
                <input type="hidden" name="token" value="{html.escape(token)}">
                <input type="hidden" name="code" value="{code}">
                <input type="hidden" name="disabled" value="{toggle_value}">
                <input type="hidden" name="reason" value="管理員手動停用">
                <button class="ghost" type="submit">{toggle_text}</button>
              </form>
              <form method="post" action="/surl/delete" style="display:inline-block" onsubmit="return confirm('確定刪除 {code}？')">
                <input type="hidden" name="token" value="{html.escape(token)}">
                <input type="hidden" name="code" value="{code}">
                <button class="danger" type="submit">刪除</button>
              </form>
            </li>""")
        list_html = "\n".join(rows) if rows else "<li class='muted'>目前還沒有短網址。</li>"
        message_html = f"<p class='notice'>{html.escape(message)}</p>" if message else ""
        token_value = html.escape(token)
        html_doc = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>kuies.tw 短網址管理</title>{self._style()}</head>
<body>
<h1>🔐 kuies.tw 短網址管理介面</h1>
<p class="muted">此管理介面僅限內網使用。</p>
{message_html}
<div class="card">
  <h2>管理模式建立短網址</h2>
  <form method="post" action="/surl/urls">
    <input name="token" type="hidden" value="{token_value}">
    <label>長網址</label>
    <input name="url" type="url" placeholder="https://example.com/very/long/url" required>
    <div class="grid">
      <div><label>短碼，建議 ≤5 字元，可留空自動產生</label><input name="code" maxlength="64" pattern="[A-Za-z0-9_-]+" placeholder="例如 pay 或 a7k2x"></div>
      <div><label>標題，可選</label><input name="title" placeholder="備註名稱"></div>
    </div>
    <label><input name="clean_tracking" type="checkbox" value="1" style="width:auto;margin-right:8px">移除追蹤參數</label>
    <button type="submit">建立短網址</button>
  </form>
</div>
<div class="card">
  <h2>已建立短網址</h2>
  <ul>{list_html}</ul>
</div>
<p><a href="/surl">離開管理介面</a></p>
</body></html>"""
        return status, {"Content-Type": "text/html; charset=utf-8"}, html_doc.encode("utf-8")


def create_app(
    store: ShortURLStore,
    base_url: str,
    admin_token: str,
    extension_zip_path: str = "",
    og_image_path: str = "",
    alert_email: str = DEFAULT_ALERT_EMAIL,
    alert_from: str = DEFAULT_ALERT_FROM,
    sendmail_path: str = DEFAULT_SENDMAIL,
    public_create_enabled: bool = True,
    public_daily_ip_limit: int = PUBLIC_DAILY_IP_LIMIT,
    preview_warm_enabled: bool = False,
) -> ShortURLApp:
    return ShortURLApp(
        store=store,
        base_url=base_url,
        admin_token=admin_token,
        extension_zip_path=extension_zip_path,
        og_image_path=og_image_path,
        alert_email=alert_email,
        alert_from=alert_from,
        sendmail_path=sendmail_path,
        public_create_enabled=public_create_enabled,
        public_daily_ip_limit=public_daily_ip_limit,
        preview_warm_enabled=preview_warm_enabled,
    )


class Handler(BaseHTTPRequestHandler):
    app: ShortURLApp

    def do_GET(self) -> None:
        self._dispatch(b"")

    def do_HEAD(self) -> None:
        self._dispatch(b"")

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0") or "0")
        self._dispatch(self.rfile.read(length))

    def do_DELETE(self) -> None:
        self._dispatch(b"")

    def do_OPTIONS(self) -> None:
        self._dispatch(b"")

    def _dispatch(self, body: bytes) -> None:
        headers = {k.lower(): v for k, v in self.headers.items()}
        forwarded = [part.strip() for part in self.headers.get("X-Forwarded-For", "").split(",") if part.strip()]
        remote_addr = self.client_address[0]
        if forwarded:
            public_hops = [addr for addr in forwarded if not is_private_client(addr)]
            remote_addr = public_hops[0] if public_hops else forwarded[0]
        status, out_headers, out_body = self.app.handle(
            self.command,
            self.path,
            headers,
            body,
            remote_addr=remote_addr,
            user_agent=self.headers.get("User-Agent", ""),
        )
        self.send_response(status)
        for key, value in out_headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(out_body)))
        self.end_headers()
        if self.command != "HEAD" and out_body:
            self.wfile.write(out_body)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="kuies.tw short URL service")
    parser.add_argument("--host", default=os.environ.get("SHORT_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SHORT_PORT", "8787")))
    parser.add_argument("--db", default=os.environ.get("SHORT_DB", "./data/shorturls.sqlite3"))
    parser.add_argument("--base-url", default=os.environ.get("SHORT_BASE_URL", "https://u.kuies.tw"))
    parser.add_argument("--admin-token", default=os.environ.get("SHORT_ADMIN_TOKEN", ""))
    parser.add_argument("--extension-zip", default=os.environ.get("SHORT_EXTENSION_ZIP", "./data/threads-link-cleaner.zip"))
    parser.add_argument("--og-image", default=os.environ.get("SHORT_OG_IMAGE", "./data/og-image.png"))
    parser.add_argument("--alert-email", default=os.environ.get("SHORT_ALERT_EMAIL", DEFAULT_ALERT_EMAIL))
    parser.add_argument("--alert-from", default=os.environ.get("SHORT_ALERT_FROM", DEFAULT_ALERT_FROM))
    parser.add_argument("--sendmail", default=os.environ.get("SHORT_ALERT_SENDMAIL", DEFAULT_SENDMAIL))
    parser.add_argument(
        "--public-create-enabled",
        default=os.environ.get("SHORT_PUBLIC_CREATE_ENABLED", "1"),
        choices=["0", "1", "false", "true", "False", "True"],
    )
    parser.add_argument("--public-daily-ip-limit", type=int, default=int(os.environ.get("SHORT_PUBLIC_DAILY_IP_LIMIT", str(PUBLIC_DAILY_IP_LIMIT))))
    args = parser.parse_args()

    if not args.admin_token:
        raise SystemExit("SHORT_ADMIN_TOKEN is required")

    store = ShortURLStore(args.db)
    Handler.app = create_app(
        store,
        args.base_url,
        args.admin_token,
        extension_zip_path=args.extension_zip,
        og_image_path=args.og_image,
        alert_email=args.alert_email,
        alert_from=args.alert_from,
        sendmail_path=args.sendmail,
        public_create_enabled=str(args.public_create_enabled).lower() not in {"0", "false"},
        public_daily_ip_limit=max(0, int(args.public_daily_ip_limit)),
        preview_warm_enabled=True,
    )
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"kuies-short-url listening on http://{args.host}:{args.port} base={args.base_url} db={args.db}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
