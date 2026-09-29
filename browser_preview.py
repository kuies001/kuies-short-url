#!/usr/bin/env python3
"""Logged-in browser fallback for Threads post previews.

Public crawler fetches of Threads post URLs frequently hit a login wall or an
author-only card. This module reuses the already logged-in Ego Lite browser to
read the post as the user would. Only public post permalinks are allowed; only
the data needed for an OG card (title / description / image URL) is returned.

Safety rails:
- whitelist: only https Threads post permalinks, no userinfo/port;
- publish gate: the logged-out visibility verdict must be "public"; posts
  restricted to logged-in viewers, deleted posts, login walls and unknown
  verdicts are all refused, so restricted cards stay author cards;
- the logged-in fetch additionally aborts when the logged-in view itself
  reports "restricted / no access / missing";
- page HTML, cookies and session state are never stored; the image is
  re-fetched by the server into its own first-party cache;
- attempts are serialized cross-process and capped per rolling hour so this
  stays a gentle background activity on the user's browser.

Diagnostics: ``check_logged_out_visibility`` renders the post in the never
logged-in CloakBrowser. Its verdict gates publishing: only "public" posts may
be fetched through the logged-in browser.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

EGO_BROWSER = str(Path.home() / ".local" / "bin" / "ego-browser")
TASK_SPACE_NAME = "kuies 短網址預覽"
BROWSER_TIMEOUT_SECONDS = 40
MIN_INTERVAL_SECONDS = 2.0
LOCK_WAIT_SECONDS = 25.0

# Cross-process attempt budget shared by the long-lived service and the refresh
# cron worker (both spawn the same Ego browser). Attempts are capped per rolling
# hour so background repair stays a gentle background activity.
ATTEMPTS_PER_HOUR = int(os.environ.get("SHORT_BROWSER_ATTEMPTS_PER_HOUR", "6"))
_STATE_DIR = Path(__file__).resolve().parent / "data"
ATTEMPT_STATE_PATH = os.environ.get("SHORT_BROWSER_ATTEMPT_STATE", str(_STATE_DIR / "browser-preview-attempts.json"))
FETCH_LOCK_PATH = os.environ.get("SHORT_BROWSER_LOCK", str(_STATE_DIR / "browser-preview-fetch.lock"))

# Only Threads post permalinks may be fetched through the logged-in browser.
_THREADS_HOSTS = {"threads.com", "www.threads.com", "threads.net", "www.threads.net"}
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{3,80}$")
_POST_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,40}$")

_LOCK = threading.Lock()
_LAST_CALL_AT = 0.0


def is_browser_preview_target(url: str) -> bool:
    """Strict whitelist: https Threads post permalink, no userinfo, no port."""
    try:
        parsed = urlparse((url or "").strip())
    except (TypeError, ValueError):
        return False
    if parsed.scheme.lower() != "https":
        return False
    if parsed.username or parsed.password or parsed.port:
        return False
    host = (parsed.hostname or "").lower()
    if host not in _THREADS_HOSTS:
        return False
    parts = [part for part in parsed.path.split("/") if part]
    # /@handle/post/<code>
    if len(parts) != 3 or not parts[0].startswith("@") or parts[1] != "post":
        return False
    return bool(_HANDLE_RE.fullmatch(parts[0][1:])) and bool(_POST_CODE_RE.fullmatch(parts[2]))


def budget_state(now: float | None = None) -> tuple[int, int]:
    """Return (used_in_window, limit) for the rolling-hour attempt budget."""
    now = time.time() if now is None else now
    return len(_read_attempts(now)), ATTEMPTS_PER_HOUR


def _read_attempts(now: float) -> list[float]:
    try:
        with open(ATTEMPT_STATE_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        attempts = [float(item) for item in (raw.get("attempts") or [])]
    except (OSError, ValueError, TypeError):
        return []
    return [item for item in attempts if now - item <= 3600]


def budget_acquire(now: float | None = None) -> bool:
    """Take one slot from the shared rolling-hour attempt budget."""
    now = time.time() if now is None else now
    try:
        os.makedirs(os.path.dirname(ATTEMPT_STATE_PATH), exist_ok=True)
        with open(ATTEMPT_STATE_PATH, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.seek(0)
                try:
                    raw = json.load(fh)
                    attempts = [float(item) for item in (raw.get("attempts") or [])]
                except (ValueError, TypeError):
                    attempts = []
                attempts = [item for item in attempts if now - item <= 3600]
                if len(attempts) >= ATTEMPTS_PER_HOUR:
                    return False
                attempts.append(now)
                fh.seek(0)
                fh.truncate()
                fh.write(json.dumps({"attempts": attempts}))
                fh.flush()
                return True
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        # State file problems must never take the preview pipeline down; fall
        # back to the in-process spacing limits only.
        return True


def reset_attempt_state() -> None:
    """Test helper: clear the shared attempt-state file."""
    try:
        os.makedirs(os.path.dirname(ATTEMPT_STATE_PATH), exist_ok=True)
        with open(ATTEMPT_STATE_PATH, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"attempts": []}))
    except OSError:
        pass


def _bootstrap_js() -> str:
    """Resolve (or create) our own Ego task space; never claim user spaces."""
    return (
        "const NAME = " + json.dumps(TASK_SPACE_NAME, ensure_ascii=False) + ";\n"
        "const spaces = await listTaskSpaces();\n"
        "const found = spaces.find(s => s.name === NAME);\n"
        "let task;\n"
        "if (found) {\n"
        "  if (found.ownership !== 'agent') throw new Error('space-not-agent-owned');\n"
        "  task = await taskSpace(found.id);\n"
        "} else {\n"
        "  task = await taskSpace(NAME);\n"
        "}\n"
    )


# Browser-side reader, evaluated twice at most: right after load, then once more
# when the post content has not rendered yet. Kept as a single JS expression.
_READER_JS = (
    "(() => {\n"
    "  const meta = (sel) => { const el = document.querySelector(sel); return el ? (el.getAttribute('content') || '') : ''; };\n"
    "  const body = document.body ? String(document.body.innerText || '') : '';\n"
    "  return {\n"
    "    href: location.href,\n"
    "    docTitle: (document.title || '').slice(0, 300),\n"
    "    ogTitle: meta('meta[property=\"og:title\"]').slice(0, 500),\n"
    "    ogDescription: meta('meta[property=\"og:description\"]').slice(0, 4000),\n"
    "    ogImage: meta('meta[property=\"og:image\"]').slice(0, 2000),\n"
    "    restricted: body.includes('此內容並未開放所有人查看') || body.includes('特定受眾無法查看'),\n"
    "    noAccess: body.includes('你必須登入') || body.includes('無法查看此內容'),\n"
    "    missing: body.includes('找不到此頁面') || body.includes(\"isn't available\") || body.includes('本頁面無法使用')\n"
    "  };\n"
    "})()"
)


def build_fetch_script(target_url: str) -> str:
    """Node script run via `ego-browser nodejs`; prints one JSON line."""
    return (
        _bootstrap_js()
        + "const target = " + json.dumps(target_url) + ";\n"
        + "const reader = String.raw`" + _READER_JS + "`;\n"
        + "let out = {status:'error', error:'not-started'};\n"
        + "let page = null;\n"
        + "try {\n"
        + "  page = await task.newPage();\n"
        + "  await page.goto(target, {waitUntil:'load', timeout:25000});\n"
        + "  await page.waitForTimeout(2600);\n"
        + "  let data = await page.evaluate(reader);\n"
        + "  if (!data.ogTitle && !data.restricted && !data.missing) {\n"
        + "    await page.waitForTimeout(1800);\n"
        + "    data = await page.evaluate(reader);\n"
        + "  }\n"
        + "  out = {status:'ok', data:data};\n"
        + "} catch (e) {\n"
        + "  out = {status:'error', error:String((e && e.message) || e).slice(0, 200)};\n"
        + "}\n"
        + "try { if (page) await page.close(); } catch (e2) {}\n"
        + "console.log(JSON.stringify(out));\n"
    )


def _generic_threads_title(title: str) -> bool:
    normalized = re.sub(r"\s+", " ", (title or "").strip())
    if not normalized:
        return True
    if normalized.casefold() in {"threads", "threads 貼文", "threads • 登入", "threads • log in", "instagram"}:
        return True
    return bool(re.match(r"^(?:Threads|Instagram)\s*(?:上的|貼文|•|·|，)", normalized, re.I))


def parse_browser_result(raw: str, target_url: str = "") -> dict:
    """Turn the Node script's stdout into a normalized result dict.

    Returns {ok, restricted, metadata, error}. ``metadata`` is only populated
    for non-restricted, non-login-wall results and contains the raw OG values.
    When ``target_url`` is given, a rendered URL that no longer points at the
    requested post permalink (e.g. Threads silently redirects deleted posts to
    the author profile) is treated as "post-missing" so we never publish a
    profile card as post content.
    """
    payload = None
    for line in reversed((raw or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            payload = value
            break
    if payload is None:
        return {"ok": False, "restricted": False, "metadata": {}, "error": "no-output"}
    if payload.get("status") != "ok":
        return {
            "ok": False,
            "restricted": False,
            "metadata": {},
            "error": str(payload.get("error") or "browser-error")[:200],
        }
    data = payload.get("data") or {}
    if data.get("restricted") or data.get("noAccess"):
        return {"ok": False, "restricted": True, "metadata": {}, "error": "restricted-audience"}
    if data.get("missing"):
        return {"ok": False, "restricted": False, "metadata": {}, "error": "post-missing"}
    if target_url:
        try:
            want = urlparse(target_url)
            got = urlparse(str(data.get("href") or ""))
            want_parts = [p for p in want.path.split("/") if p]
            got_parts = [p for p in got.path.split("/") if p]
            if not got_parts:
                return {"ok": False, "restricted": False, "metadata": {}, "error": "page-not-loaded"}
            if (got.hostname or "").lower() not in _THREADS_HOSTS or got_parts[:3] != want_parts[:3]:
                return {"ok": False, "restricted": False, "metadata": {}, "error": "redirected-away"}
        except (TypeError, ValueError):
            return {"ok": False, "restricted": False, "metadata": {}, "error": "bad-href"}
    og_title = str(data.get("ogTitle") or "").strip()
    og_description = str(data.get("ogDescription") or "").strip()
    og_image = str(data.get("ogImage") or "").strip()
    doc_title = str(data.get("docTitle") or "").strip()
    # Prefer the rendered document title when the OG title is only the author
    # line and the document title carries the actual post text.
    title = og_title
    if _generic_threads_title(og_title) and not _generic_threads_title(doc_title) and len(doc_title) >= 4:
        title = doc_title
    if not (title or og_description) or not og_image:
        return {"ok": False, "restricted": False, "metadata": {}, "error": "incomplete-metadata"}
    if _generic_threads_title(title) and not og_description:
        return {"ok": False, "restricted": False, "metadata": {}, "error": "generic-title-only"}
    return {
        "ok": True,
        "restricted": False,
        "metadata": {"title": title, "description": og_description, "image": og_image},
        "error": "",
    }


def _run_ego(script: str, timeout: int = BROWSER_TIMEOUT_SECONDS) -> str:
    if not os.path.isfile(EGO_BROWSER):
        raise FileNotFoundError("ego-browser-not-installed")
    proc = subprocess.run(
        [EGO_BROWSER, "nodejs"],
        input=script,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def _cross_process_lock():
    """Serialize whole browser fetches across processes (best-effort)."""
    try:
        os.makedirs(os.path.dirname(FETCH_LOCK_PATH), exist_ok=True)
        fh = open(FETCH_LOCK_PATH, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        return None


def fetch_threads_post_metadata(target_url: str) -> dict:
    """Fetch raw OG metadata for one Threads post through the logged-in browser.

    Serialized in-process and cross-process, gently rate limited; every failure
    mode returns a structured result instead of raising so preview warm can
    degrade safely.
    """
    global _LAST_CALL_AT
    if not is_browser_preview_target(target_url):
        return {"ok": False, "restricted": False, "metadata": {}, "error": "not-allowed"}
    acquired = _LOCK.acquire(timeout=LOCK_WAIT_SECONDS)
    if not acquired:
        return {"ok": False, "restricted": False, "metadata": {}, "error": "busy"}
    try:
        cross_lock = _cross_process_lock()
        if cross_lock is None:
            return {"ok": False, "restricted": False, "metadata": {}, "error": "busy-other-process"}
        try:
            wait_for = MIN_INTERVAL_SECONDS - (time.monotonic() - _LAST_CALL_AT)
            if wait_for > 0:
                time.sleep(wait_for)
            script = build_fetch_script(target_url)
            try:
                raw = _run_ego(script)
            except subprocess.TimeoutExpired:
                return {"ok": False, "restricted": False, "metadata": {}, "error": "timeout"}
            except Exception as exc:  # noqa: BLE001 - never let browser issues crash warm
                return {"ok": False, "restricted": False, "metadata": {}, "error": type(exc).__name__}
            finally:
                _LAST_CALL_AT = time.monotonic()
            return parse_browser_result(raw, target_url=target_url)
        finally:
            try:
                fcntl.flock(cross_lock.fileno(), fcntl.LOCK_UN)
            finally:
                cross_lock.close()
    finally:
        _LOCK.release()


def fetch_threads_preview(target_url: str, use_budget: bool = True, gate_verdict: str | None = None) -> dict:
    """Budgeted + gated wrapper used by the preview warm pipeline and repair tool.

    The logged-out visibility probe decides whether the logged-in fetch may
    publish at all: only posts a logged-out visitor can actually read
    ("public") are eligible. "restricted" means Threads itself says the content
    is not open to everyone, so those cards stay author cards; "missing" posts
    are gone; "login-wall" / "unknown" verdicts cannot prove public visibility
    and are refused as well. Callers may pass a pre-computed ``gate_verdict``
    (e.g. from an earlier probe) to avoid rendering the post twice.
    """
    result = {"ok": False, "restricted": False, "metadata": {}, "gate": "skipped", "error": ""}
    if not is_browser_preview_target(target_url):
        result["error"] = "not-allowed"
        return result
    if use_budget and not budget_acquire():
        result["error"] = "budget-exhausted"
        return result
    if gate_verdict is None:
        probe = check_logged_out_visibility(target_url)
        visibility = str(probe.get("visibility") or "unknown") if isinstance(probe, dict) else "unknown"
    else:
        visibility = str(gate_verdict or "unknown")
    result["gate"] = visibility
    result["restricted"] = visibility == "restricted"
    if visibility != "public":
        result["error"] = f"gate-{visibility}"
        return result
    fetched = fetch_threads_post_metadata(target_url)
    if fetched.get("restricted"):
        result["restricted"] = True
        result["error"] = "restricted-audience"
        return result
    result["ok"] = bool(fetched.get("ok"))
    result["metadata"] = fetched.get("metadata") or {}
    result["error"] = fetched.get("error") or ""
    return result


# Logged-out visibility probe: rendered by CloakBrowser (never logged in).
# Informational diagnostics only (repair tooling / investigations); it never
# gates the automatic background pipeline.
VISIBILITY_PYTHON = os.environ.get(
    "SHORT_VISIBILITY_PYTHON",
    str(Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"),
)
VISIBILITY_SCRIPT = os.environ.get(
    "SHORT_VISIBILITY_SCRIPT",
    str(Path(__file__).resolve().parent / "scripts" / "cloak_visibility_check.py"),
)
VISIBILITY_TIMEOUT_SECONDS = 75.0


def check_logged_out_visibility(target_url: str) -> dict:
    """Probe what a logged-out visitor sees for one Threads post (diagnostic)."""
    if not is_browser_preview_target(target_url):
        return {"ok": False, "visibility": "error", "detail": "not-allowed"}
    if not os.path.isfile(VISIBILITY_PYTHON) or not os.path.isfile(VISIBILITY_SCRIPT):
        return {"ok": False, "visibility": "error", "detail": "probe-unavailable"}
    try:
        proc = subprocess.run(
            [VISIBILITY_PYTHON, VISIBILITY_SCRIPT, target_url],
            capture_output=True,
            text=True,
            timeout=VISIBILITY_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "visibility": "error", "detail": "timeout"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "visibility": "error", "detail": type(exc).__name__}
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "visibility" in value:
            return value
    return {"ok": False, "visibility": "error", "detail": "no-output"}
