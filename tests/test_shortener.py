import json
import hashlib
import os
import subprocess
import tempfile
import time
import unittest
import zipfile
from urllib.error import HTTPError
from urllib.parse import quote

import app as shortener_app
from app import (
    ShortURLStore,
    clean_threads_url,
    clean_tracking_url,
    create_app,
    canonicalize_target_url,
    is_blocked_target_url,
    is_private_client,
    is_safe_url,
    make_code,
    needs_warning_page,
    sanitize_preview_metadata,
    target_hash_for,
)


class ShortURLTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "shorturls.sqlite3")
        self.store = ShortURLStore(self.db_path)
        self.original_preview_dir = shortener_app.PREVIEW_IMAGE_DIR
        self.original_fallback_preview_dir = shortener_app.FALLBACK_PREVIEW_IMAGE_DIR
        shortener_app.PREVIEW_IMAGE_DIR = os.path.join(self.tmp.name, "preview-images")
        shortener_app.FALLBACK_PREVIEW_IMAGE_DIR = os.path.join(self.tmp.name, "fallback-images")
        os.makedirs(shortener_app.PREVIEW_IMAGE_DIR, exist_ok=True)
        os.makedirs(shortener_app.FALLBACK_PREVIEW_IMAGE_DIR, exist_ok=True)

    def tearDown(self):
        shortener_app.PREVIEW_IMAGE_DIR = self.original_preview_dir
        shortener_app.FALLBACK_PREVIEW_IMAGE_DIR = self.original_fallback_preview_dir
        self.tmp.cleanup()

    def test_make_code_is_short_and_url_safe(self):
        codes = [make_code(5) for _ in range(200)]
        for code in codes:
            self.assertEqual(len(code), 5)
            self.assertRegex(code, r"^[A-Za-z0-9]+$")
            self.assertNotIn("_", code)

    def test_auto_generated_codes_start_at_three_chars(self):
        row = self.store.create_url("https://example.com/auto-short")
        self.assertEqual(len(row["code"]), 3)

    def test_auto_generated_codes_fall_back_to_four_chars_after_collisions(self):
        self.store.create_url("https://example.com/existing", code="aaa")
        original_make_code = shortener_app.make_code
        try:
            shortener_app.make_code = lambda length: "aaa" if length == 3 else "bbbb"
            row = self.store.create_url("https://example.com/fallback")
        finally:
            shortener_app.make_code = original_make_code
        self.assertEqual(row["code"], "bbbb")

    def test_rejects_unsafe_urls(self):
        for url in ["javascript:alert(1)", "file:///etc/passwd", "data:text/html,x", "ftp://example.com/file", "not-a-url"]:
            self.assertFalse(is_safe_url(url), url)
        self.assertTrue(is_safe_url("https://example.com/path?q=1"))
        self.assertTrue(is_safe_url("http://example.com"))

    def test_blocks_internal_self_and_shortener_targets_for_public_creation(self):
        blocked = [
            "http://127.0.0.1:8787/private",
            "http://192.168.1.33/status",
            "http://localhost:8787",
            "http://nas.local/share",
            "http://printer/admin",
            "https://u.kuies.tw/abc",
            "https://bit.ly/example",
            "https://reurl.cc/abc123",
        ]
        for url in blocked:
            is_blocked, reason = is_blocked_target_url(url, base_url="https://u.kuies.tw")
            self.assertTrue(is_blocked, f"{url} should be blocked")
            self.assertTrue(reason)
        is_blocked, reason = is_blocked_target_url("https://example.com/article", base_url="https://u.kuies.tw")
        self.assertFalse(is_blocked, reason)

    def test_clean_threads_url_removes_only_xmt_param(self):
        self.assertEqual(
            clean_threads_url("https://www.threads.net/@abc/post/123?xmt=AQGz&igsh=keep"),
            "https://www.threads.net/@abc/post/123?igsh=keep",
        )
        self.assertEqual(
            clean_threads_url("https://www.threads.com/@abc/post/123?xmt=AQGz&igsh=keep"),
            "https://www.threads.com/@abc/post/123?igsh=keep",
        )
        self.assertEqual(
            clean_threads_url("https://threads.com/@abc/post/123?xmt=AQGz"),
            "https://threads.com/@abc/post/123",
        )
        self.assertEqual(
            clean_threads_url("https://www.threads.net/@abc/post/123?xmt=AQGz"),
            "https://www.threads.net/@abc/post/123",
        )
        self.assertEqual(
            clean_threads_url("https://example.com/path?xmt=keep"),
            "https://example.com/path?xmt=keep",
        )

    def test_clean_tracking_url_removes_threads_xmt_and_instagram_tracking_params(self):
        self.assertEqual(
            clean_tracking_url("https://www.threads.com/@abc/post/123?xmt=AQGz&igsh=keep"),
            "https://www.threads.com/@abc/post/123?igsh=keep",
        )
        self.assertEqual(
            clean_tracking_url("https://www.instagram.com/p/ABC/?utm_source=ig_web_copy_link&igsh=remove&utm_medium=social&utm_campaign=camp&foo=keep"),
            "https://www.instagram.com/p/ABC/?foo=keep",
        )
        self.assertEqual(
            clean_tracking_url("https://example.com/path?utm_source=keep&igsh=keep&xmt=keep"),
            "https://example.com/path?utm_source=keep&igsh=keep&xmt=keep",
        )

    def test_clean_tracking_url_removes_threads_share_redirect_params(self):
        self.assertEqual(
            clean_tracking_url("https://www.threads.com/@abc/post/XYZ?xmt=tracking&slof=1"),
            "https://www.threads.com/@abc/post/XYZ",
        )

    def test_resolve_threads_share_url_uses_clean_canonical_redirect_location(self):
        class RedirectOnlyOpener:
            def open(self, request, timeout):
                raise HTTPError(
                    request.full_url,
                    302,
                    "Found",
                    {"Location": "https://www.threads.com/@abc/post/XYZ?xmt=tracking&slof=1"},
                    None,
                )

        resolved = shortener_app.resolve_threads_share_url(
            "https://www.threads.com/share/_x7pY2G9V/",
            opener=RedirectOnlyOpener(),
        )
        self.assertEqual(resolved, "https://www.threads.com/@abc/post/XYZ")

    def test_prepare_target_url_resolves_new_threads_share_path_before_shortening(self):
        original_resolver = shortener_app.resolve_threads_share_url
        try:
            shortener_app.resolve_threads_share_url = lambda url: "https://www.threads.com/@abc/post/XYZ?xmt=tracking&slof=1"
            cleaned = shortener_app.prepare_target_url(
                "https://www.threads.com/share/_x7pY2G9V/",
                clean_tracking=True,
            )
        finally:
            shortener_app.resolve_threads_share_url = original_resolver
        self.assertEqual(cleaned, "https://www.threads.com/@abc/post/XYZ")

    def test_clean_tracking_url_removes_facebook_share_redirect_params(self):
        self.assertEqual(
            clean_tracking_url(
                "https://www.facebook.com/permalink.php?story_fbid=pfbid123&id=61562231634394&rdid=random&share_url=https%3A%2F%2Fwww.facebook.com%2Fshare%2Fp%2Ftoken%2F&fbclid=tracking"
            ),
            "https://www.facebook.com/permalink.php?story_fbid=pfbid123&id=61562231634394",
        )

    def test_resolve_facebook_share_url_uses_clean_same_site_redirect_location(self):
        class RedirectOnlyOpener:
            def open(self, request, timeout):
                raise HTTPError(
                    request.full_url,
                    302,
                    "Found",
                    {
                        "Location": "https://www.facebook.com/permalink.php?story_fbid=pfbid123&id=61562231634394&rdid=random&share_url=tracking"
                    },
                    None,
                )

        resolved = shortener_app.resolve_facebook_share_url(
            "https://www.facebook.com/share/p/1bCsBTnWp7/",
            opener=RedirectOnlyOpener(),
        )
        self.assertEqual(
            resolved,
            "https://www.facebook.com/permalink.php?story_fbid=pfbid123&id=61562231634394",
        )

    def test_prepare_target_url_resolves_facebook_share_path_before_shortening(self):
        original_resolver = shortener_app.resolve_facebook_share_url
        try:
            shortener_app.resolve_facebook_share_url = lambda url: (
                "https://www.facebook.com/permalink.php?story_fbid=pfbid123&id=61562231634394&rdid=random&share_url=tracking"
            )
            cleaned = shortener_app.prepare_target_url(
                "https://www.facebook.com/share/p/1bCsBTnWp7/",
                clean_tracking=True,
            )
        finally:
            shortener_app.resolve_facebook_share_url = original_resolver
        self.assertEqual(
            cleaned,
            "https://www.facebook.com/permalink.php?story_fbid=pfbid123&id=61562231634394",
        )

    def test_private_client_detection(self):
        for addr in ["127.0.0.1", "192.168.1.88", "10.0.0.5", "172.16.0.2", "fd00::1", "::1"]:
            self.assertTrue(is_private_client(addr), addr)
        self.assertFalse(is_private_client("8.8.8.8"))

    def test_create_and_lookup_short_url(self):
        row = self.store.create_url("https://example.com/a/very/long/url", code="demo")
        self.assertEqual(row["code"], "demo")
        self.assertEqual(row["target_url"], "https://example.com/a/very/long/url")
        found = self.store.lookup("demo")
        self.assertEqual(found["target_url"], "https://example.com/a/very/long/url")

    def test_duplicate_custom_code_is_rejected(self):
        self.store.create_url("https://example.com/one", code="same")
        with self.assertRaisesRegex(ValueError, "短碼「same」已被使用"):
            self.store.create_url("https://example.com/two", code="same")

    def test_duplicate_custom_code_error_lists_suggestions(self):
        self.store.create_url("https://example.com/one", code="dns")
        original_make_code = shortener_app.make_code
        generated = iter(["a1b", "c2d", "e3f"])
        try:
            shortener_app.make_code = lambda length: next(generated)
            with self.assertRaisesRegex(ValueError, "建議短碼：a1b、c2d、e3f"):
                self.store.create_url("https://example.com/two", code="dns")
        finally:
            shortener_app.make_code = original_make_code

    def test_reserved_route_code_is_rejected(self):
        original_make_code = shortener_app.make_code
        generated = iter(["r1a", "r2b", "r3c"])
        try:
            shortener_app.make_code = lambda length: next(generated)
            with self.assertRaisesRegex(ValueError, "系統保留路徑"):
                self.store.create_url("https://example.com/route-conflict", code="surl")
        finally:
            shortener_app.make_code = original_make_code
        self.assertIsNone(self.store.lookup("surl"))

    def test_auto_generated_code_skips_reserved_routes(self):
        original_make_code = shortener_app.make_code
        generated = iter(["surl", "ok1"])
        try:
            shortener_app.make_code = lambda length: next(generated)
            row = self.store.create_url("https://example.com/reserved-skip")
        finally:
            shortener_app.make_code = original_make_code
        self.assertEqual(row["code"], "ok1")

    def test_lookup_increments_clicks(self):
        self.store.create_url("https://example.com", code="hit")
        self.store.record_click("hit", user_agent="unittest", remote_addr="127.0.0.1")
        found = self.store.lookup("hit")
        self.assertEqual(found["clicks"], 1)

    def test_http_create_requires_token(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        status, headers, body = app.handle("POST", "/api/urls", {}, b'{"url":"https://example.com"}', "127.0.0.1", "test")
        self.assertEqual(status, 401)

    def test_http_create_and_redirect(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        payload = json.dumps({"url": "https://example.com/long", "code": "ex"}).encode()
        status, headers, body = app.handle("POST", "/api/urls", {"authorization": "Bearer secret"}, payload, "127.0.0.1", "test")
        self.assertEqual(status, 201)
        data = json.loads(body.decode())
        self.assertEqual(data["short_url"], "https://u.kuies.tw/ex")

        status, headers, body = app.handle("GET", "/ex", {}, b"", "127.0.0.1", "test")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "https://example.com/long")

    def test_head_short_url_redirects_without_counting_click(self):
        self.store.create_url("https://example.com/head", code="hd")
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        status, headers, body = app.handle("HEAD", "/hd", {}, b"", "127.0.0.1", "curl/8.0")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "https://example.com/head")
        self.assertEqual(body, b"")
        self.assertEqual(self.store.lookup("hd")["clicks"], 0)

    def test_preview_crawler_head_redirects_without_counting_click(self):
        self.store.create_url("https://example.com/head", code="hdbg", title="預覽測試")
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        status, headers, body = app.handle("HEAD", "/hdbg", {}, b"", "127.0.0.1", "facebookexternalhit")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "https://example.com/head")
        self.assertEqual(self.store.lookup("hdbg")["clicks"], 0)

    def test_head_healthz_is_supported(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        status, headers, body = app.handle("HEAD", "/healthz", {}, b"", "127.0.0.1", "test")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")

    def test_extension_privacy_policy_page_is_public(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        status, headers, body = app.handle("GET", "/privacy/threads-link-cleaner", {}, b"", "8.8.8.8", "test")
        html = body.decode()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("kuies.tw Short URL 隱私權政策", html)
        self.assertIn("https://u.kuies.tw/api/public/shorten", html)

    def test_gkd_icashpay_guide_is_public_android_only_and_app_scoped(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        status, headers, body = app.handle("GET", "/gkd", {}, b"", "8.8.8.8", "Mozilla/5.0")
        html = body.decode()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("Android 限定", html)
        self.assertIn("https://github.com/gkd-kit/gkd/releases", html)
        self.assertIn("首頁 → 訂閱 → 本地訂閱 → 應用規則", html)
        self.assertIn("tw.com.icash.a.icashpay", html)
        self.assertIn('[vid=&quot;bottom_view&quot;]', html)
        self.assertIn("不是全域規則", html)
        self.assertIn("Flip7 封面小螢幕", html)
        self.assertIn("無障礙服務", html)
        self.assertIn("停用或移除", html)

    def test_gkd_guide_short_code_is_reserved(self):
        with self.assertRaisesRegex(ValueError, "系統保留路徑"):
            self.store.create_url("https://example.com/should-not-shadow-guide", code="gkd")

    def test_urlcheck_social_catalog_merges_official_rules_and_adds_safe_social_tracking_rules(self):
        upstream = {
            "providers": {
                "amazon": {"urlPattern": "amazon", "rules": ["qid"]},
                "facebook": {"urlPattern": "facebook", "rules": ["mibextid"]},
                "instagram": {"urlPattern": "instagram", "rules": ["igsh"]},
            }
        }
        catalog = shortener_app.build_urlcheck_social_catalog(upstream)
        providers = catalog["providers"]

        self.assertIn("amazon", providers)
        self.assertTrue({"xmt", "slof"}.issubset(set(providers["threads"]["rules"])))
        self.assertTrue({"igsh", "igshid"}.issubset(set(providers["instagram"]["rules"])))
        self.assertTrue({"mibextid", "rdid", "share_url"}.issubset(set(providers["facebook"]["rules"])))
        self.assertRegex("https://www.threads.com/@abc/post/XYZ?xmt=tracking", providers["threads"]["urlPattern"])
        self.assertRegex("https://www.instagram.com/p/ABC/?igsh=tracking", providers["instagram"]["urlPattern"])
        self.assertRegex("https://www.facebook.com/permalink.php?story_fbid=p&id=1", providers["facebook"]["urlPattern"])

        # These identify the actual post/media and must never be stripped.
        for semantic_key in ("story_fbid", "id", "v", "img_index", "comment_id"):
            self.assertNotIn(semantic_key, providers["facebook"]["rules"])
            self.assertNotIn(semantic_key, providers["instagram"]["rules"])
            self.assertNotIn(semantic_key, providers["threads"]["rules"])

    def test_urlcheck_social_catalog_and_hash_are_public_and_consistent(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        status, headers, body = app.handle("GET", "/ucl", {}, b"", "8.8.8.8", "URLCheck")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        catalog = json.loads(body.decode("utf-8"))
        self.assertIn("threads", catalog["providers"])

        hash_status, hash_headers, hash_body = app.handle("GET", "/uch", {}, b"", "8.8.8.8", "URLCheck")
        self.assertEqual(hash_status, 200)
        self.assertEqual(hash_headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(hash_body.decode("ascii").strip(), hashlib.sha256(body).hexdigest())

    def test_urlcheck_social_catalog_paths_are_reserved(self):
        for code in ("ucl", "uch"):
            with self.assertRaisesRegex(ValueError, "系統保留路徑"):
                self.store.create_url("https://example.com/should-not-shadow-catalog", code=code)

    def test_urlcheck_social_resolver_redirects_facebook_share_wrapper_to_canonical_post(self):
        original = shortener_app.resolve_facebook_share_url
        shortener_app.resolve_facebook_share_url = lambda url: (
            "https://www.facebook.com/groups/1667374137341717/permalink/2328906167855174/"
        )
        try:
            app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
            source = "https://www.facebook.com/share/p/1Yr54CtKUe/"
            status, headers, body = app.handle(
                "GET", f"/sr?url={quote(source, safe='')}", {}, b"", "8.8.8.8", "URLCheck"
            )
        finally:
            shortener_app.resolve_facebook_share_url = original

        self.assertEqual(status, 302)
        self.assertEqual(
            headers["Location"],
            "https://www.facebook.com/groups/1667374137341717/permalink/2328906167855174/",
        )
        self.assertEqual(body, b"")

    def test_urlcheck_social_resolver_rejects_non_social_and_unresolved_wrappers(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        status, _, _ = app.handle(
            "GET",
            f"/sr?url={quote('https://example.com/', safe='')}",
            {}, b"", "8.8.8.8", "URLCheck",
        )
        self.assertEqual(status, 400)

        original = shortener_app.resolve_facebook_share_url
        shortener_app.resolve_facebook_share_url = lambda url: url
        try:
            source = "https://www.facebook.com/share/p/1Yr54CtKUe/"
            status, _, _ = app.handle(
                "GET", f"/sr?url={quote(source, safe='')}", {}, b"", "8.8.8.8", "URLCheck"
            )
        finally:
            shortener_app.resolve_facebook_share_url = original
        self.assertEqual(status, 422)

    def test_urlcheck_social_resolver_pattern_catalog_is_public_and_paths_are_reserved(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        status, headers, body = app.handle("GET", "/ucp", {}, b"", "8.8.8.8", "URLCheck")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        patterns = json.loads(body.decode("utf-8"))
        resolver_names = {"Kuies：還原 Facebook 分享網址", "Kuies：還原 Threads 分享網址"}
        self.assertTrue(resolver_names.issubset(set(patterns)))
        self.assertIn("非 ASCII 字元", patterns)
        self.assertIn("HTTP 網址", patterns)
        for name in resolver_names:
            pattern = patterns[name]
            self.assertTrue(pattern["encode"])
            self.assertTrue(pattern["automatic"])
            self.assertEqual(pattern["replacement"], "https://u.kuies.tw/sr?url=$0")

        for code in ("sr", "ucp"):
            with self.assertRaisesRegex(ValueError, "系統保留路徑"):
                self.store.create_url("https://example.com/should-not-shadow-resolver", code=code)

    def test_preview_crawler_gets_first_party_open_graph_card_for_general_target(self):
        self.store.create_url("https://example.com/article", code="gen1", title="一般網頁標題")
        original_fetch = shortener_app.fetch_open_graph_metadata
        original_cache = shortener_app.cache_preview_image
        shortener_app.fetch_open_graph_metadata = lambda url: {
            "title": "原站文章標題",
            "description": "原站文章摘要",
            "image": "https://cdn.example.com/card.jpg",
        }
        cached_image = os.path.join(shortener_app.PREVIEW_IMAGE_DIR, "gen1.jpg")
        with open(cached_image, "wb") as fh:
            fh.write(b"fake-jpeg")
        shortener_app.cache_preview_image = lambda code, image_url, timeout=8: (cached_image, "image/jpeg")
        try:
            app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
            status, headers, body = app.handle(
                "GET", "/gen1", {}, b"", "8.8.8.8", "facebookexternalhit/1.1"
            )
        finally:
            shortener_app.fetch_open_graph_metadata = original_fetch
            shortener_app.cache_preview_image = original_cache
        html = body.decode()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn('property="og:site_name" content="example.com"', html)
        self.assertIn('property="og:title" content="原站文章標題"', html)
        self.assertIn('property="og:description" content="原站文章摘要"', html)
        self.assertRegex(html, r'property="og:image" content="https://u\.kuies\.tw/preview-image/gen1-[0-9a-f]{12}\.jpg\?v=2"')
        self.assertEqual(self.store.lookup("gen1")["clicks"], 0)

    def test_preview_crawler_general_target_falls_back_to_stored_title_and_default_image(self):
        self.store.create_url("https://example.com/no-og", code="gen2", title="手動輸入標題")
        original_fetch = shortener_app.fetch_open_graph_metadata
        original_cache = shortener_app.cache_preview_image
        shortener_app.fetch_open_graph_metadata = lambda url: {}
        shortener_app.cache_preview_image = lambda code, image_url, timeout=8: (None, None)
        try:
            app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
            status, headers, body = app.handle(
                "GET", "/gen2", {}, b"", "8.8.8.8", "facebookexternalhit/1.1"
            )
        finally:
            shortener_app.fetch_open_graph_metadata = original_fetch
            shortener_app.cache_preview_image = original_cache
        html = body.decode()
        self.assertEqual(status, 200)
        self.assertIn('property="og:title" content="手動輸入標題"', html)
        self.assertRegex(html, r'property="og:image" content="https://u\.kuies\.tw/preview-image/gen2-[0-9a-f]{12}\.png\?v=2"')

    def test_common_social_preview_bots_get_first_party_open_graph_page(self):
        self.store.create_url("https://www.threads.com/@abc/post/preview", code="bots", title="已移除追蹤參數的分享連結")
        original_fetch = shortener_app.fetch_open_graph_metadata
        original_cache = shortener_app.cache_preview_image
        shortener_app.fetch_open_graph_metadata = lambda url: {}
        shortener_app.cache_preview_image = lambda code, image_url, timeout=8: (None, None)
        try:
            app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
            for user_agent in [
                "TelegramBot (like TwitterBot)",
                "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)",
                "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)",
                "Twitterbot/1.0",
                "LinkedInBot/1.0",
                "WhatsApp/2.24",
            ]:
                status, headers, body = app.handle("GET", "/bots", {}, b"", "8.8.8.8", user_agent)
                self.assertEqual(status, 200, user_agent)
                self.assertRegex(body.decode(), r'property="og:image" content="https://u\.kuies\.tw/preview-image/bots-[0-9a-f]{12}\.png\?v=2"')
        finally:
            shortener_app.fetch_open_graph_metadata = original_fetch
            shortener_app.cache_preview_image = original_cache

    def test_preview_cache_lookup_checks_external_and_fallback_directories(self):
        external_dir = os.path.join(self.tmp.name, "external")
        fallback_dir = os.path.join(self.tmp.name, "fallback")
        os.makedirs(external_dir)
        os.makedirs(fallback_dir)
        fallback_image = os.path.join(fallback_dir, "split.jpg")
        with open(fallback_image, "wb") as fh:
            fh.write(b"fallback-image")
        original_external = shortener_app.PREVIEW_IMAGE_DIR
        original_fallback = shortener_app.FALLBACK_PREVIEW_IMAGE_DIR
        shortener_app.PREVIEW_IMAGE_DIR = external_dir
        shortener_app.FALLBACK_PREVIEW_IMAGE_DIR = fallback_dir
        try:
            cached_path, content_type = shortener_app._preview_image_cache_path("split")
        finally:
            shortener_app.PREVIEW_IMAGE_DIR = original_external
            shortener_app.FALLBACK_PREVIEW_IMAGE_DIR = original_fallback
        self.assertEqual(cached_path, fallback_image)
        self.assertEqual(content_type, "image/jpeg")

    def test_social_preview_warm_uses_public_profile_when_post_metadata_is_unavailable(self):
        self.store.create_url(
            "https://www.threads.com/@example_user/post/unavailable",
            code="profilefb",
            title="已移除追蹤參數的分享連結",
        )
        app = create_app(
            self.store,
            base_url="https://u.kuies.tw",
            admin_token="secret",
            preview_warm_enabled=True,
        )
        original_fetch = shortener_app.fetch_open_graph_metadata
        original_cache = shortener_app.cache_preview_image
        fetched_urls = []
        cached_image = os.path.join(shortener_app.PREVIEW_IMAGE_DIR, "profilefb.jpg")
        with open(cached_image, "wb") as fh:
            fh.write(b"profile-image")

        def fetch_metadata(url):
            fetched_urls.append(url)
            if "/post/" in url:
                return {
                    "title": "Threads • Log in",
                    "description": "Log in with your Instagram.",
                    "image": "https://static.cdninstagram.com/login.webp",
                }
            return {
                "title": "範例用戶（@example_user） • Threads，暢所欲言",
                "description": "範例用戶，分享 AI 工具與產品實戰。",
                "image": "https://scontent.cdninstagram.com/profile.jpg",
            }

        shortener_app.fetch_open_graph_metadata = fetch_metadata
        shortener_app.cache_preview_image = lambda code, image_url, timeout=8: (cached_image, "image/jpeg")
        try:
            row = self.store.lookup("profilefb")
            assert row is not None
            app._warm_preview(row)
        finally:
            shortener_app.fetch_open_graph_metadata = original_fetch
            shortener_app.cache_preview_image = original_cache

        refreshed = self.store.lookup("profilefb")
        assert refreshed is not None
        self.assertEqual(
            fetched_urls,
            [
                "https://www.threads.com/@example_user/post/unavailable",
                "https://www.threads.com/@example_user",
            ],
        )
        self.assertEqual(refreshed["preview_status"], "profile_fallback")
        self.assertEqual(refreshed["preview_title"], "Threads 貼文｜範例用戶（@example_user）")
        self.assertIn("分享 AI 工具", refreshed["preview_description"])
        status, _, body = app.handle(
            "GET", "/profilefb", {}, b"", "8.8.8.8", "facebookexternalhit/1.1"
        )
        preview_html = body.decode()
        self.assertEqual(status, 200)
        self.assertIn('property="og:title" content="Threads 貼文｜範例用戶（@example_user）"', preview_html)
        self.assertIn("範例用戶，分享 AI 工具與產品實戰。", preview_html)
        self.assertNotIn("Log in with your Instagram", preview_html)

    def test_social_profile_fallback_keeps_generic_fallback_when_profile_has_no_metadata(self):
        self.store.create_url(
            "https://www.threads.com/@missing/post/unavailable",
            code="genericfb",
            title="已移除追蹤參數的分享連結",
        )
        app = create_app(
            self.store,
            base_url="https://u.kuies.tw",
            admin_token="secret",
            preview_warm_enabled=True,
        )
        original_fetch = shortener_app.fetch_open_graph_metadata
        original_cache = shortener_app.cache_preview_image
        fetched_urls = []
        shortener_app.fetch_open_graph_metadata = lambda url: fetched_urls.append(url) or {
            "title": "", "description": "", "image": ""
        }
        shortener_app.cache_preview_image = lambda code, image_url, timeout=8: (None, None)
        try:
            row = self.store.lookup("genericfb")
            assert row is not None
            app._warm_preview(row)
        finally:
            shortener_app.fetch_open_graph_metadata = original_fetch
            shortener_app.cache_preview_image = original_cache

        refreshed = self.store.lookup("genericfb")
        assert refreshed is not None
        self.assertEqual(refreshed["preview_status"], "fallback")
        self.assertEqual(refreshed["preview_title"], "")
        self.assertEqual(refreshed["preview_description"], "")
        self.assertEqual(
            fetched_urls,
            [
                "https://www.threads.com/@missing/post/unavailable",
                "https://www.threads.com/@missing",
            ],
        )

    def test_social_profile_fallback_never_fetches_userinfo_custom_port_or_non_post_path(self):
        original_fetch = shortener_app.fetch_open_graph_metadata
        fetched_urls = []
        shortener_app.fetch_open_graph_metadata = lambda url: fetched_urls.append(url) or {
            "title": "不應取得",
            "description": "不應取得",
            "image": "",
        }
        try:
            unsafe_targets = [
                "https://evil.example@www.threads.com/@abc/post/123",
                "https://www.threads.com:444/@abc/post/123",
                "https://www.threads.com:notaport/@abc/post/123",
                "http://www.threads.com/@abc/post/123",
                "https://www.threads.com/@abc",
                "https://www.threads.com/@abc/other/123",
            ]
            for target in unsafe_targets:
                self.assertEqual(shortener_app.fetch_social_profile_fallback_metadata(target), {})
        finally:
            shortener_app.fetch_open_graph_metadata = original_fetch
        self.assertEqual(fetched_urls, [])

    def test_warmed_social_preview_is_served_without_live_source_fetch(self):
        self.store.create_url("https://www.threads.com/@abc/post/warmed", code="warm", title="已移除追蹤參數的分享連結")
        app = create_app(
            self.store,
            base_url="https://u.kuies.tw",
            admin_token="secret",
            preview_warm_enabled=True,
        )
        original_fetch = shortener_app.fetch_open_graph_metadata
        original_cache = shortener_app.cache_preview_image
        cached_image = os.path.join(shortener_app.PREVIEW_IMAGE_DIR, "warm.jpg")
        with open(cached_image, "wb") as fh:
            fh.write(b"cached-image")
        shortener_app.fetch_open_graph_metadata = lambda url: {
            "title": "Threads 上的 小明（@abc）",
            "description": "預先快取的貼文內容",
            "image": "https://scontent.cdninstagram.com/warm.jpg",
        }
        shortener_app.cache_preview_image = lambda code, image_url, timeout=8: (cached_image, "image/jpeg")
        try:
            app._warm_preview(self.store.lookup("warm"))
            shortener_app.fetch_open_graph_metadata = lambda url: (_ for _ in ()).throw(AssertionError("crawler request must not fetch Threads live"))
            status, headers, body = app.handle(
                "GET", "/warm", {}, b"", "8.8.8.8", "TelegramBot (like TwitterBot)"
            )
        finally:
            shortener_app.fetch_open_graph_metadata = original_fetch
            shortener_app.cache_preview_image = original_cache
        html = body.decode()
        self.assertEqual(status, 200)
        self.assertIn('property="og:title" content="預先快取的貼文內容"', html)
        self.assertIn('property="og:description" content="Threads 上的 小明（@abc）"', html)
        refreshed = self.store.lookup("warm")
        self.assertEqual(refreshed["preview_status"], "ready")
        self.assertTrue(refreshed["preview_updated_at"])

    def test_preview_crawler_gets_original_social_open_graph_card(self):
        self.store.create_url("https://www.threads.com/@abc/post/123", code="th1", title="已移除追蹤參數的分享連結")
        original_fetch = shortener_app.fetch_open_graph_metadata
        original_cache = shortener_app.cache_preview_image
        shortener_app.fetch_open_graph_metadata = lambda url: {
            "title": "原始 Threads 貼文標題",
            "description": "原始 Threads 貼文內容摘要",
            "image": "https://instagram.examplecdn.test/original.jpg?x=1&y=2",
        }
        cached_image = os.path.join(shortener_app.PREVIEW_IMAGE_DIR, "th1.jpg")
        with open(cached_image, "wb") as fh:
            fh.write(b"fake-jpeg")
        shortener_app.cache_preview_image = lambda code, image_url, timeout=8: (cached_image, "image/jpeg")
        try:
            app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
            status, headers, body = app.handle(
                "GET",
                "/th1",
                {},
                b"",
                "8.8.8.8",
                "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
            )
            image_status, image_headers, image_body = app.handle(
                "GET", "/preview-image/th1.jpg", {}, b"", "8.8.8.8", "facebookexternalhit"
            )
        finally:
            shortener_app.fetch_open_graph_metadata = original_fetch
            shortener_app.cache_preview_image = original_cache
        html = body.decode()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn('property="og:title" content="原始 Threads 貼文標題"', html)
        self.assertIn('property="og:description" content="原始 Threads 貼文內容摘要"', html)
        self.assertRegex(html, r'property="og:image" content="https://u\.kuies\.tw/preview-image/th1-[0-9a-f]{12}\.jpg\?v=2"')
        self.assertRegex(html, r'property="og:image:secure_url" content="https://u\.kuies\.tw/preview-image/th1-[0-9a-f]{12}\.jpg\?v=2"')
        self.assertIn('name="twitter:card" content="summary_large_image"', html)
        self.assertRegex(html, r'name="twitter:image" content="https://u\.kuies\.tw/preview-image/th1-[0-9a-f]{12}\.jpg\?v=2"')
        self.assertRegex(html, r'<img src="https://u\.kuies\.tw/preview-image/th1-[0-9a-f]{12}\.jpg\?v=2"')
        self.assertIn('property="og:image:type" content="image/jpeg"', html)
        self.assertNotIn("已移除追蹤參數的分享連結", html)
        self.assertEqual(image_status, 200)
        self.assertEqual(image_headers["Content-Type"], "image/jpeg")
        self.assertEqual(image_body, b"fake-jpeg")
        self.assertEqual(self.store.lookup("th1")["clicks"], 0)

    def test_preview_card_uses_cached_image_dimensions_and_schema_cache_buster(self):
        self.store.create_url("https://www.threads.com/@abc/post/dimensions", code="dim1")
        image_path = os.path.join(shortener_app.PREVIEW_IMAGE_DIR, "dim1.png")
        # Minimal PNG header declaring the same 588x588 size as the affected card.
        with open(image_path, "wb") as fh:
            fh.write(bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452"
                "0000024c0000024c0806000000"
            ))
        self.store.update_preview_metadata(
            "dim1",
            {"title": "Threads 貼文｜作者", "description": "貼文說明"},
            "profile_fallback",
            image_path=image_path,
        )
        app = create_app(self.store, "https://u.kuies.tw", "secret", preview_warm_enabled=True)

        status, _, body = app.handle(
            "GET",
            "/dim1",
            {},
            b"",
            "8.8.8.8",
            "facebookexternalhit/1.1",
        )

        html = body.decode()
        self.assertEqual(status, 200)
        self.assertRegex(html, r"/preview-image/dim1-[0-9a-f]{12}\.png\?v=2")
        self.assertIn('property="og:image:width" content="588"', html)
        self.assertIn('property="og:image:height" content="588"', html)

    def test_preview_crawler_uses_post_text_as_title_when_original_title_is_author(self):
        self.store.create_url("https://www.threads.com/@abc/post/456", code="th2", title="已移除追蹤參數的分享連結")
        original_fetch = shortener_app.fetch_open_graph_metadata
        original_cache = shortener_app.cache_preview_image
        shortener_app.fetch_open_graph_metadata = lambda url: {
            "title": "Threads 上的 小明（@abc）",
            "description": "這才是真正想讓 Messenger 顯示的貼文內容",
            "image": "https://instagram.examplecdn.test/original.jpg",
        }
        cached_image = os.path.join(shortener_app.PREVIEW_IMAGE_DIR, "th2.jpg")
        with open(cached_image, "wb") as fh:
            fh.write(b"fake-jpeg")
        shortener_app.cache_preview_image = lambda code, image_url, timeout=8: (cached_image, "image/jpeg")
        try:
            app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
            status, headers, body = app.handle(
                "GET", "/th2", {}, b"", "8.8.8.8", "facebookexternalhit/1.1"
            )
        finally:
            shortener_app.fetch_open_graph_metadata = original_fetch
            shortener_app.cache_preview_image = original_cache
        html = body.decode()
        self.assertEqual(status, 200)
        self.assertIn('property="og:title" content="這才是真正想讓 Messenger 顯示的貼文內容"', html)
        self.assertIn('property="og:description" content="Threads 上的 小明（@abc）"', html)

    def test_social_login_wall_metadata_is_sanitized(self):
        metadata = {
            "title": "Threads • 登入",
            "description": "加入 Threads 即可分享意見。使用你的 Instagram 登入。",
            "image": "https://static.cdninstagram.com/rsrc.php/yd/r/kHwIMM5b8PW.webp",
        }
        self.assertEqual(sanitize_preview_metadata("https://www.threads.com/@abc/post/123", metadata), {})

    def test_preview_crawler_does_not_publish_threads_login_card(self):
        self.store.create_url("https://www.threads.com/@abc/post/789", code="thlog", title="已移除追蹤參數的分享連結")
        original_fetch = shortener_app.fetch_open_graph_metadata
        original_cache = shortener_app.cache_preview_image
        shortener_app.fetch_open_graph_metadata = lambda url: {
            "title": "Threads • 登入",
            "description": "加入 Threads 即可分享意見、詢問問題。使用你的 Instagram 登入。",
            "image": "https://static.cdninstagram.com/rsrc.php/yd/r/kHwIMM5b8PW.webp",
        }
        cache_calls = []
        shortener_app.cache_preview_image = lambda code, image_url, timeout=8: cache_calls.append(image_url) or (None, None)
        try:
            app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
            status, headers, body = app.handle(
                "GET", "/thlog", {}, b"", "8.8.8.8", "facebookexternalhit/1.1"
            )
        finally:
            shortener_app.fetch_open_graph_metadata = original_fetch
            shortener_app.cache_preview_image = original_cache
        html = body.decode()
        self.assertEqual(status, 200)
        self.assertIn('property="og:title" content="Threads 貼文｜@abc"', html)
        self.assertRegex(html, r'property="og:image" content="https://u\.kuies\.tw/preview-image/thlog-[0-9a-f]{12}\.png\?v=2"')
        self.assertNotIn("Threads • 登入", html)
        self.assertNotIn("static.cdninstagram.com", html)
        self.assertEqual(cache_calls, [""])

    def test_regular_browser_still_redirects_short_url(self):
        self.store.create_url("https://www.instagram.com/p/ABC/", code="ig1")
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        status, headers, body = app.handle("GET", "/ig1", {}, b"", "8.8.8.8", "Mozilla/5.0")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "https://www.instagram.com/p/ABC/")

    def test_og_image_route_serves_png(self):
        image_path = os.path.join(self.tmp.name, "og-image.png")
        with open(image_path, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\nplaceholder")
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret", og_image_path=image_path)
        status, headers, body = app.handle("GET", "/og-image.png", {}, b"", "8.8.8.8", "test")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertTrue(body.startswith(b"\x89PNG"))

    def test_external_surl_page_has_public_shortener_only(self):
        self.store.create_url("https://example.com/private", code="priv")
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        status, headers, body = app.handle("GET", "/surl", {}, b"", "8.8.8.8", "test")
        html = body.decode()
        self.assertEqual(status, 200)
        self.assertIn("縮短網址", html)
        self.assertNotIn("管理金鑰", html)
        self.assertNotIn("已建立短網址", html)
        self.assertNotIn("https://u.kuies.tw/priv", html)
        self.assertNotIn("https://example.com/private", html)

    def test_admin_route_is_removed(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        status, headers, body = app.handle("GET", "/admin", {}, b"", "192.168.1.50", "test")
        self.assertEqual(status, 404)

    def test_external_public_form_creates_url_and_shows_copy_button(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        token = app._public_form_token()
        body = f"url=https%3A%2F%2Fexample.com%2Fpublic&code=p1&title=Public&anti_bot_token={token}".encode()
        status, headers, response = app.handle("POST", "/shorten", {"content-type": "application/x-www-form-urlencoded"}, body, "8.8.8.8", "test")
        html = response.decode()
        self.assertEqual(status, 200)
        self.assertIn("https://u.kuies.tw/p1", html)
        self.assertIn("複製網址", html)
        self.assertNotIn("已建立短網址", html)
        self.assertEqual(self.store.lookup("p1")["target_url"], "https://example.com/public")

    def test_external_public_form_rejects_reserved_route_code(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        token = app._public_form_token()
        body = f"url=https%3A%2F%2Fexample.com%2Fbug&code=surl&title=Bug&anti_bot_token={token}".encode()
        status, headers, response = app.handle("POST", "/shorten", {"content-type": "application/x-www-form-urlencoded"}, body, "8.8.8.8", "test")
        html = response.decode()
        self.assertEqual(status, 400)
        self.assertIn("短碼「surl」是系統保留路徑", html)
        self.assertIn("或將短碼欄位留空", html)
        self.assertNotIn("https://u.kuies.tw/surl", html)
        self.assertIsNone(self.store.lookup("surl"))

    def test_external_public_form_rejects_duplicate_code_with_suggestions(self):
        self.store.create_url("https://example.com/existing", code="dns")
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        token = app._public_form_token()
        original_make_code = shortener_app.make_code
        generated = iter(["x1a", "x2b", "x3c"])
        try:
            shortener_app.make_code = lambda length: next(generated)
            body = f"url=https%3A%2F%2Fexample.com%2Fbug&code=dns&title=Bug&anti_bot_token={token}".encode()
            status, headers, response = app.handle("POST", "/shorten", {"content-type": "application/x-www-form-urlencoded"}, body, "8.8.8.8", "test")
        finally:
            shortener_app.make_code = original_make_code
        html = response.decode()
        self.assertEqual(status, 400)
        self.assertIn("短碼「dns」已被使用", html)
        self.assertIn("建議短碼：x1a、x2b、x3c", html)
        self.assertIn("或將短碼欄位留空", html)
        self.assertNotIn("已成功縮短", html)

    def test_form_option_cleans_tracking_before_shortening(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        token = app._public_form_token()
        body = f"url=https%3A%2F%2Fwww.instagram.com%2Fp%2FABC%2F%3Futm_source%3Dig_web_copy_link%26igsh%3Dremove%26foo%3Dkeep&code=igf&clean_tracking=1&anti_bot_token={token}".encode()
        status, headers, response = app.handle("POST", "/shorten", {"content-type": "application/x-www-form-urlencoded"}, body, "8.8.8.8", "test")
        self.assertEqual(status, 200)
        self.assertEqual(self.store.lookup("igf")["target_url"], "https://www.instagram.com/p/ABC/?foo=keep")

    def test_public_json_api_cleans_threads_xmt_and_returns_short_url(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        payload = json.dumps({
            "url": "https://www.threads.com/@abc/post/123?xmt=AQGz&igsh=keep",
            "code": "t1234",
            "clean_threads_xmt": True,
        }).encode()
        status, headers, body = app.handle("POST", "/api/public/shorten", {"content-type": "application/json"}, payload, "8.8.8.8", "test")
        data = json.loads(body.decode())
        self.assertEqual(status, 201)
        self.assertEqual(data["short_url"], "https://u.kuies.tw/t1234")
        self.assertEqual(data["target_url"], "https://www.threads.com/@abc/post/123?igsh=keep")
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")

    def test_public_json_api_warms_social_preview_before_return_to_avoid_generic_cached_title(self):
        app = create_app(
            self.store,
            base_url="https://u.kuies.tw",
            admin_token="secret",
            preview_warm_enabled=True,
        )
        original_fetch = shortener_app.fetch_open_graph_metadata
        original_cache = shortener_app.cache_preview_image
        cached_image = os.path.join(shortener_app.PREVIEW_IMAGE_DIR, "racex.jpg")
        with open(cached_image, "wb") as fh:
            fh.write(b"cached-image")

        def delayed_metadata(url):
            time.sleep(0.15)
            return {
                "title": "Threads 上的 小明（@abc）",
                "description": "通訊軟體第一次抓取就應看到的貼文標題",
                "image": "https://scontent.cdninstagram.com/race.jpg",
            }

        shortener_app.fetch_open_graph_metadata = delayed_metadata
        shortener_app.cache_preview_image = lambda code, image_url, timeout=8: (cached_image, "image/jpeg")
        try:
            payload = json.dumps({
                "url": "https://www.threads.com/@abc/post/preview-race?xmt=tracking",
                "code": "racex",
                "clean_tracking": True,
            }).encode()
            started = time.monotonic()
            status, headers, body = app.handle(
                "POST", "/api/public/shorten", {"content-type": "application/json"}, payload, "8.8.8.8", "test"
            )
            elapsed = time.monotonic() - started
            data = json.loads(body.decode())
            preview_status, _, preview_body = app.handle(
                "GET", f"/{data['code']}", {}, b"", "8.8.8.8", "TelegramBot (like TwitterBot)"
            )
        finally:
            shortener_app.fetch_open_graph_metadata = original_fetch
            shortener_app.cache_preview_image = original_cache

        preview_html = preview_body.decode()
        self.assertEqual(status, 201)
        self.assertGreaterEqual(elapsed, 0.12)
        self.assertEqual(preview_status, 200)
        self.assertIn('property="og:title" content="通訊軟體第一次抓取就應看到的貼文標題"', preview_html)
        self.assertNotIn('property="og:title" content="Threads 分享連結"', preview_html)
        self.assertEqual(self.store.lookup(data["code"])["preview_status"], "ready")

    def test_public_json_api_uses_page_preview_for_social_login_wall_targets(self):
        app = create_app(
            self.store,
            base_url="https://u.kuies.tw",
            admin_token="secret",
            preview_warm_enabled=True,
        )
        original_fetch = shortener_app.fetch_open_graph_metadata
        original_profile = shortener_app.fetch_social_profile_fallback_metadata
        original_cache = shortener_app.cache_preview_image
        cached_image = os.path.join(shortener_app.PREVIEW_IMAGE_DIR, "cprev.jpg")
        with open(cached_image, "wb") as fh:
            fh.write(b"client-preview")

        shortener_app.fetch_open_graph_metadata = lambda url: {}
        shortener_app.fetch_social_profile_fallback_metadata = lambda url: {}
        shortener_app.cache_preview_image = (
            lambda code, image_url, timeout=8, force=False: (cached_image, "image/jpeg")
        )
        try:
            payload = json.dumps({
                "url": "https://www.threads.com/@abc/post/client-preview?xmt=tracking",
                "code": "cprev",
                "clean_tracking": True,
                "preview": {
                    "title": "這是範例貼文的內容用來驗證預覽擷取",
                    "description": "這是範例貼文的內容用來驗證預覽擷取 🫣🫣",
                    "image": "https://scontent.cdninstagram.com/v/t51.71878-15/post.jpg?signature=temporary",
                },
            }).encode()
            status, _, body = app.handle(
                "POST",
                "/api/public/shorten",
                {"content-type": "application/json"},
                payload,
                "8.8.8.8",
                "extension-test",
            )
            data = json.loads(body.decode())
            row = self.store.lookup(data["code"])
            preview_status, _, preview_body = app.handle(
                "GET",
                f"/{data['code']}",
                {},
                b"",
                "8.8.8.8",
                "facebookexternalhit/1.1",
            )
        finally:
            shortener_app.fetch_open_graph_metadata = original_fetch
            shortener_app.fetch_social_profile_fallback_metadata = original_profile
            shortener_app.cache_preview_image = original_cache

        preview_html = preview_body.decode()
        self.assertEqual(status, 201)
        self.assertEqual(row["preview_status"], "ready")
        self.assertEqual(row["preview_title"], "這是範例貼文的內容用來驗證預覽擷取")
        self.assertEqual(preview_status, 200)
        self.assertIn("這是範例貼文的內容用來驗證預覽擷取", preview_html)
        self.assertIn("/preview-image/", preview_html)

    def test_public_json_api_fast_response_returns_before_social_preview_warmup_finishes(self):
        app = create_app(
            self.store,
            base_url="https://u.kuies.tw",
            admin_token="secret",
            preview_warm_enabled=True,
        )
        original_fetch = shortener_app.fetch_open_graph_metadata
        original_cache = shortener_app.cache_preview_image
        cached_image = os.path.join(shortener_app.PREVIEW_IMAGE_DIR, "fast-race.jpg")
        with open(cached_image, "wb") as fh:
            fh.write(b"cached-image")

        def delayed_metadata(url):
            time.sleep(0.25)
            return {
                "title": "Threads 上的 小明（@abc）",
                "description": "背景完成的快速模式貼文預覽",
                "image": "https://scontent.cdninstagram.com/fast-race.jpg",
            }

        shortener_app.fetch_open_graph_metadata = delayed_metadata
        shortener_app.cache_preview_image = lambda code, image_url, timeout=8: (cached_image, "image/jpeg")
        try:
            payload = json.dumps({
                "url": "https://www.threads.com/@abc/post/fast-response?xmt=tracking",
                "clean_tracking": True,
                "fast_response": True,
            }).encode()
            started = time.monotonic()
            status, headers, body = app.handle(
                "POST", "/api/public/shorten", {"content-type": "application/json"}, payload, "8.8.8.8", "extension-test"
            )
            elapsed = time.monotonic() - started
            data = json.loads(body.decode())
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                row = self.store.lookup(data["code"])
                if row and row.get("preview_status") == "ready":
                    break
                time.sleep(0.02)
            else:
                self.fail("快速回應後的背景預覽暖機沒有完成")
        finally:
            shortener_app.fetch_open_graph_metadata = original_fetch
            shortener_app.cache_preview_image = original_cache

        self.assertEqual(status, 201)
        self.assertLess(elapsed, 0.12)
        self.assertTrue(data["short_url"].startswith("https://u.kuies.tw/"))
        self.assertEqual(self.store.lookup(data["code"])["preview_status"], "ready")

    def test_fast_response_first_meta_crawler_waits_for_background_preview(self):
        app = create_app(
            self.store,
            base_url="https://u.kuies.tw",
            admin_token="secret",
            preview_warm_enabled=True,
        )
        app.preview_crawler_wait_seconds = 1.0
        original_fetch = shortener_app.fetch_open_graph_metadata
        original_cache = shortener_app.cache_preview_image
        cached_image = os.path.join(shortener_app.PREVIEW_IMAGE_DIR, "firstm.jpg")
        with open(cached_image, "wb") as fh:
            fh.write(b"cached-image")

        def delayed_metadata(url):
            time.sleep(0.25)
            return {
                "title": "Threads 上的範例作者（@example_author）",
                "description": "Messenger 第一次抓取就應看到的貼文內容",
                "image": "https://scontent.cdninstagram.com/first-meta.jpg",
            }

        shortener_app.fetch_open_graph_metadata = delayed_metadata
        shortener_app.cache_preview_image = lambda code, image_url, timeout=8: (cached_image, "image/jpeg")
        try:
            payload = json.dumps({
                "url": "https://www.threads.com/@example_author/post/first-meta?xmt=tracking",
                "code": "firstm",
                "clean_tracking": True,
                "fast_response": True,
            }).encode()
            post_started = time.monotonic()
            post_status, _, post_body = app.handle(
                "POST",
                "/api/public/shorten",
                {"content-type": "application/json"},
                payload,
                "8.8.8.8",
                "extension-test",
            )
            post_elapsed = time.monotonic() - post_started
            data = json.loads(post_body.decode())

            crawler_status, _, crawler_body = app.handle(
                "GET",
                f"/{data['code']}",
                {},
                b"",
                "8.8.8.8",
                "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
            )

            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                row = self.store.lookup(data["code"])
                if row and row.get("preview_status") == "ready":
                    break
                time.sleep(0.02)
            else:
                self.fail("快速回應後的背景預覽暖機沒有完成")
        finally:
            shortener_app.fetch_open_graph_metadata = original_fetch
            shortener_app.cache_preview_image = original_cache

        preview_html = crawler_body.decode()
        self.assertEqual(post_status, 201)
        self.assertLess(post_elapsed, 0.12)
        self.assertEqual(data["preview_status"], "pending")
        self.assertEqual(crawler_status, 200)
        self.assertIn(
            'property="og:title" content="Messenger 第一次抓取就應看到的貼文內容"',
            preview_html,
        )
        self.assertNotIn('property="og:title" content="Threads 貼文｜@example_author"', preview_html)
        final_row = self.store.lookup(data["code"])
        assert final_row is not None
        self.assertEqual(final_row["preview_status"], "ready")

    def test_pending_preview_timeout_returns_retryable_status_without_fallback_card(self):
        app = create_app(
            self.store,
            base_url="https://u.kuies.tw",
            admin_token="secret",
            preview_warm_enabled=True,
        )
        app.preview_crawler_wait_seconds = 0.04
        original_fetch = shortener_app.fetch_open_graph_metadata
        original_cache = shortener_app.cache_preview_image
        cached_image = os.path.join(shortener_app.PREVIEW_IMAGE_DIR, "late-meta.jpg")
        with open(cached_image, "wb") as fh:
            fh.write(b"cached-image")

        def delayed_metadata(url):
            time.sleep(0.2)
            return {
                "title": "Threads 上的範例作者（@example_author）",
                "description": "稍後完成的預覽",
                "image": "https://scontent.cdninstagram.com/late-meta.jpg",
            }

        shortener_app.fetch_open_graph_metadata = delayed_metadata
        shortener_app.cache_preview_image = lambda code, image_url, timeout=8: (cached_image, "image/jpeg")
        try:
            payload = json.dumps({
                "url": "https://www.threads.com/@example_author/post/late-meta?xmt=tracking",
                "clean_tracking": True,
                "fast_response": True,
            }).encode()
            _, _, post_body = app.handle(
                "POST",
                "/api/public/shorten",
                {"content-type": "application/json"},
                payload,
                "8.8.8.8",
                "extension-test",
            )
            code = json.loads(post_body.decode())["code"]
            crawler_status, crawler_headers, crawler_body = app.handle(
                "GET",
                f"/{code}",
                {},
                b"",
                "8.8.8.8",
                "meta-externalagent/1.1 (+https://developers.facebook.com/docs/sharing/webmasters/crawler)",
            )

            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                row = self.store.lookup(code)
                if row and row.get("preview_status") == "ready":
                    break
                time.sleep(0.02)
            else:
                self.fail("逾時回應後的背景預覽暖機沒有完成")
        finally:
            shortener_app.fetch_open_graph_metadata = original_fetch
            shortener_app.cache_preview_image = original_cache

        self.assertEqual(crawler_status, 503)
        self.assertEqual(crawler_headers["Retry-After"], "2")
        self.assertEqual(crawler_headers["Cache-Control"], "no-store")
        self.assertNotIn(b"og:title", crawler_body)
        self.assertNotIn("Threads 貼文｜@example_author", crawler_body.decode())

    def test_degraded_social_preview_waits_before_first_crawler_card(self):
        app = create_app(
            self.store,
            base_url="https://u.kuies.tw",
            admin_token="secret",
            preview_warm_enabled=True,
        )
        app.preview_crawler_wait_seconds = 1.0
        target_url = "https://www.threads.com/@example_author/post/degraded-crawler"
        self.store.create_url(target_url, code="degraded-crawler")
        self.store.update_preview_metadata(
            "degraded-crawler",
            {"title": "Threads 貼文", "description": "", "image": ""},
            "fallback",
        )
        original_fetch = shortener_app.fetch_open_graph_metadata
        original_cache = shortener_app.cache_preview_image
        cached_image = os.path.join(shortener_app.PREVIEW_IMAGE_DIR, "degraded-crawler.jpg")
        with open(cached_image, "wb") as fh:
            fh.write(b"cached-image")

        def delayed_metadata(url):
            time.sleep(0.15)
            return {
                "title": "Threads 上的範例作者（@example_author）",
                "description": "降級狀態也必須先完成的貼文預覽",
                "image": "https://scontent.cdninstagram.com/degraded-crawler.jpg",
            }

        shortener_app.fetch_open_graph_metadata = delayed_metadata
        shortener_app.cache_preview_image = lambda code, image_url, timeout=8: (cached_image, "image/jpeg")
        try:
            started = time.monotonic()
            status, headers, body = app.handle(
                "GET",
                "/degraded-crawler",
                {},
                b"",
                "8.8.8.8",
                "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
            )
            elapsed = time.monotonic() - started
        finally:
            shortener_app.fetch_open_graph_metadata = original_fetch
            shortener_app.cache_preview_image = original_cache

        html = body.decode()
        self.assertEqual(status, 200)
        self.assertGreaterEqual(elapsed, 0.12)
        self.assertIn('property="og:title" content="降級狀態也必須先完成的貼文預覽"', html)
        self.assertNotIn('property="og:title" content="Threads 貼文"', html)
        final_row = self.store.lookup("degraded-crawler")
        self.assertIsNotNone(final_row)
        assert final_row is not None
        self.assertEqual(final_row["preview_status"], "ready")

    def test_public_json_api_shortens_any_url_without_cleaning(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        payload = json.dumps({
            "url": "https://example.com/a/very/long/path?utm_source=keep&xmt=keep",
            "code": "any1",
        }).encode()
        status, headers, body = app.handle("POST", "/api/public/shorten", {"content-type": "application/json"}, payload, "8.8.8.8", "test")
        data = json.loads(body.decode())
        self.assertEqual(status, 201)
        self.assertEqual(data["short_url"], "https://u.kuies.tw/any1")
        self.assertEqual(data["target_url"], "https://example.com/a/very/long/path?utm_source=keep&xmt=keep")
        self.assertEqual(data["source"], "extension")
        self.assertEqual(data["creator_ip"], "8.8.8.8")

    def test_public_json_api_rejects_blocked_targets(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        for url in ["http://127.0.0.1:8787/private", "https://u.kuies.tw/self", "https://bit.ly/abc"]:
            payload = json.dumps({"url": url}).encode()
            status, headers, body = app.handle("POST", "/api/public/shorten", {"content-type": "application/json"}, payload, "8.8.8.8", "test")
            data = json.loads(body.decode())
            self.assertEqual(status, 400, url)
            self.assertIn("error", data)

    def test_public_create_emergency_switch_blocks_external_but_not_lan(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret", public_create_enabled=False)
        payload = json.dumps({"url": "https://example.com/blocked"}).encode()
        status, headers, body = app.handle("POST", "/api/public/shorten", {"content-type": "application/json"}, payload, "8.8.8.8", "test")
        self.assertEqual(status, 503)
        data = json.loads(body.decode())
        self.assertIn("暫時關閉", data["error"])

        lan_payload = json.dumps({"url": "https://example.com/lan", "code": "lanok"}).encode()
        status, headers, body = app.handle("POST", "/api/public/shorten", {"content-type": "application/json"}, lan_payload, "192.168.1.50", "test")
        self.assertEqual(status, 201)

    def test_public_daily_ip_limit_blocks_after_quota(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret", public_daily_ip_limit=1)
        first = json.dumps({"url": "https://example.com/one", "code": "one1"}).encode()
        status, headers, body = app.handle("POST", "/api/public/shorten", {"content-type": "application/json"}, first, "8.8.4.4", "ua")
        self.assertEqual(status, 201)
        second = json.dumps({"url": "https://example.com/two", "code": "two1"}).encode()
        status, headers, body = app.handle("POST", "/api/public/shorten", {"content-type": "application/json"}, second, "8.8.4.4", "different-ua")
        self.assertEqual(status, 429)
        self.assertIn("今日", json.loads(body.decode())["error"])

    def test_disabled_short_url_returns_410_until_reenabled(self):
        self.store.create_url("https://example.com/disable", code="off")
        self.assertTrue(self.store.set_disabled("off", disabled=True, reason="abuse"))
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        status, headers, body = app.handle("GET", "/off", {}, b"", "8.8.8.8", "Mozilla/5.0")
        self.assertEqual(status, 410)
        self.assertEqual(json.loads(body.decode())["error"], "short_url_disabled")
        self.assertTrue(self.store.set_disabled("off", disabled=False))
        status, headers, body = app.handle("GET", "/off", {}, b"", "8.8.8.8", "Mozilla/5.0")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "https://example.com/disable")


    def test_canonicalize_target_url_removes_tracking_and_sorts_query(self):
        one = "https://WWW.Example.com/path?b=2&utm_source=x&a=1#frag"
        two = "https://example.com/path?a=1&b=2"
        self.assertEqual(canonicalize_target_url(one), canonicalize_target_url(two))
        self.assertEqual(target_hash_for(one), target_hash_for(two))

    def test_high_risk_keywords_trigger_warning_but_normal_unknown_domain_redirects(self):
        self.assertTrue(needs_warning_page("https://example.com/login?verify=password"))
        self.assertFalse(needs_warning_page("https://unknown-example.com/article/hello"))
        self.store.create_url("https://unknown-example.com/article/hello", code="safeu")
        self.store.create_url("https://unknown-example.com/login?verify=password", code="risk1")
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        status, headers, body = app.handle("GET", "/safeu", {}, b"", "8.8.8.8", "Mozilla/5.0")
        self.assertEqual(status, 302)
        status, headers, body = app.handle("GET", "/risk1", {}, b"", "8.8.8.8", "Mozilla/5.0")
        html = body.decode()
        self.assertEqual(status, 200)
        self.assertIn("高風險連結提醒", html)
        self.assertIn("/go/risk1", html)
        status, headers, body = app.handle("GET", "/go/risk1", {}, b"", "8.8.8.8", "Mozilla/5.0")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "https://unknown-example.com/login?verify=password")

    def test_report_page_is_public(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        status, headers, body = app.handle("GET", "/report", {}, b"", "8.8.8.8", "Mozilla/5.0")
        html = body.decode()
        self.assertEqual(status, 200)
        self.assertIn("檢舉 u.kuies.tw 短網址", html)
        self.assertIn("short-url@example.com", html)

    def test_public_json_api_deduplicates_same_canonical_target(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        first = json.dumps({"url": "https://example.com/post?b=2&utm_source=x&a=1"}).encode()
        second = json.dumps({"url": "https://www.example.com/post?a=1&b=2#frag"}).encode()
        status, headers, body = app.handle("POST", "/api/public/shorten", {"content-type": "application/json"}, first, "8.8.8.8", "ua1")
        self.assertEqual(status, 201)
        first_data = json.loads(body.decode())
        status, headers, body = app.handle("POST", "/api/public/shorten", {"content-type": "application/json"}, second, "8.8.8.8", "ua2")
        self.assertEqual(status, 201)
        second_data = json.loads(body.decode())
        self.assertEqual(first_data["short_url"], second_data["short_url"])
        self.assertEqual(self.store.count_urls(), 1)

    def test_disabled_target_hash_does_not_get_recreated(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        payload = json.dumps({"url": "https://example.com/phish?login=1"}).encode()
        status, headers, body = app.handle("POST", "/api/public/shorten", {"content-type": "application/json"}, payload, "8.8.8.8", "ua1")
        self.assertEqual(status, 201)
        code = json.loads(body.decode())["code"]
        self.store.set_disabled(code, disabled=True, reason="abuse")
        status, headers, body = app.handle("POST", "/api/public/shorten", {"content-type": "application/json"}, payload, "8.8.4.4", "ua2")
        self.assertEqual(status, 400)
        self.assertIn("曾被停用", json.loads(body.decode())["error"])

    def test_abuse_alerts_for_single_ip_and_domain(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        sent = []
        app._send_alert_email = lambda subject, body: sent.append((subject, body)) or True
        for idx in range(19):
            self.store.create_url(f"https://burst.example.com/{idx}", code=f"b{idx}", source="extension", creator_ip="9.9.9.9")
        app._create_and_alert("https://burst.example.com/19", code="b19", source="extension", creator_ip="9.9.9.9")
        subjects = "\n".join(subject for subject, body in sent)
        self.assertIn("單一 IP", subjects)
        self.assertIn("單一網域", subjects)

    def test_public_json_api_cleans_tracking_params(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        payload = json.dumps({
            "url": "https://www.instagram.com/p/ABC/?utm_source=ig_web_copy_link&igsh=remove&foo=keep",
            "code": "ig123",
            "clean_tracking": True,
        }).encode()
        status, headers, body = app.handle("POST", "/api/public/shorten", {"content-type": "application/json"}, payload, "8.8.8.8", "test")
        data = json.loads(body.decode())
        self.assertEqual(status, 201)
        self.assertEqual(data["short_url"], "https://u.kuies.tw/ig123")
        self.assertEqual(data["target_url"], "https://www.instagram.com/p/ABC/?foo=keep")

    def test_root_redirects_to_shortener_page_for_get_and_head(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                status, headers, body = app.handle(method, "/", {}, b"", "8.8.8.8", "test")
                self.assertEqual(status, 302)
                self.assertEqual(headers["Location"], "https://u.kuies.tw/surl")
                self.assertEqual(body, b"")

    def test_surl_page_links_chrome_extension_store_and_footer_privacy_policy(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        status, headers, body = app.handle("GET", "/surl", {}, b"", "8.8.8.8", "test")
        html = body.decode()
        self.assertEqual(status, 200)
        self.assertIn("點此下載安裝 Chrome 擴充", html)
        self.assertIn('href="https://chromewebstore.google.com/detail/kuiestw-short-url/icbadaliljifnlpgnadgiekcfeiblgdh"', html)
        self.assertNotIn('href="/downloads/threads-link-cleaner.zip"', html)
        privacy_link = 'href="/privacy/threads-link-cleaner"'
        self.assertIn(privacy_link, html)
        self.assertGreater(html.rfind(privacy_link), html.index("點此下載安裝 Chrome 擴充"))
        self.assertLess(html.rfind(privacy_link), html.rfind("</body>"))

    def test_chrome_extension_zip_download(self):
        zip_path = os.path.join(self.tmp.name, "threads-link-cleaner.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("threads-link-cleaner/manifest.json", "{}")
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret", extension_zip_path=zip_path)
        status, headers, body = app.handle("GET", "/downloads/threads-link-cleaner.zip", {}, b"", "8.8.8.8", "test")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertTrue(body.startswith(b"PK"))

    def test_private_dns_mobileconfig_download(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        status, headers, body = app.handle("GET", "/downloads/kuies-private-dns-dot.mobileconfig", {}, b"", "8.8.8.8", "test")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/x-apple-aspen-config")
        self.assertIn(b"dns.kuies.tw", body)
        self.assertIn(b"com.apple.dnsSettings.managed", body)

    def test_chrome_extension_uses_store_safe_public_api_only(self):
        root = os.path.dirname(os.path.dirname(__file__))
        manifest_path = os.path.join(root, "extension", "threads-link-cleaner", "manifest.json")
        content_path = os.path.join(root, "extension", "threads-link-cleaner", "content.js")
        popup_path = os.path.join(root, "extension", "threads-link-cleaner", "popup.js")
        page_path = os.path.join(root, "extension", "threads-link-cleaner", "page-interceptor.js")

        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        with open(content_path, encoding="utf-8") as fh:
            content = fh.read()
        with open(popup_path, encoding="utf-8") as fh:
            popup = fh.read()
        with open(page_path, encoding="utf-8") as fh:
            page = fh.read()
        combined = "\n".join([content, popup, page, json.dumps(manifest)])

        self.assertEqual(manifest["version"], "1.7.5")
        self.assertIn("https://u.kuies.tw/*", manifest["host_permissions"])
        self.assertIn("https://www.facebook.com/*", manifest["host_permissions"])
        self.assertNotIn("192.168.", combined)
        self.assertNotIn("127.0.0.1", combined)
        self.assertIn("window.postMessage", content)
        self.assertIn("window.postMessage", page)
        self.assertIn("kuies-content-script", content)
        self.assertIn("kuies-page-interceptor", page)
        self.assertIn("SHORTENER_API = 'https://u.kuies.tw/api/public/shorten'", content)
        self.assertIn("fast_response: true", content)
        self.assertIn("SHORTENER_API = 'https://u.kuies.tw/api/public/shorten'", popup)

    def test_page_interceptor_first_copy_waits_for_short_url_before_writing_clipboard(self):
        script_path = os.path.join(
            os.path.dirname(__file__),
            "test_page_interceptor_first_copy.js",
        )
        result = subprocess.run(
            ["node", script_path],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"Node regression test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        self.assertIn("first-copy-short-url-and-facebook-share: ok", result.stdout)

    def test_page_interceptor_extracts_post_text_and_ignores_profile_avatar(self):
        script_path = os.path.join(
            os.path.dirname(__file__),
            "test_page_interceptor_preview_metadata.js",
        )
        result = subprocess.run(
            ["node", script_path],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"Node 預覽擷取測試失敗：\nSTDOUT：\n{result.stdout}\nSTDERR：\n{result.stderr}",
        )
        self.assertIn("page-preview-metadata: ok", result.stdout)

    def test_page_interceptor_first_copy_does_not_wait_for_preview_timeout(self):
        script_path = os.path.join(
            os.path.dirname(__file__),
            "test_page_interceptor_timeout_race.js",
        )
        result = subprocess.run(
            ["node", script_path],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"Node timeout race regression test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        self.assertIn("first-copy-timeout-race-and-api-fallback: ok", result.stdout)

    def test_page_interceptor_preserves_threads_native_close_and_only_handles_real_copy(self):
        script_path = os.path.join(
            os.path.dirname(__file__),
            "test_page_interceptor_native_ui.js",
        )
        result = subprocess.run(
            ["node", script_path],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"Node native UI regression test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        self.assertIn("native-close-click-and-real-copy-only: ok", result.stdout)

    def test_internal_surl_page_shows_manage_entry_but_not_list_before_login(self):
        self.store.create_url("https://example.com/private", code="priv")
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        status, headers, body = app.handle("GET", "/surl", {}, b"", "192.168.1.50", "test")
        html = body.decode()
        self.assertEqual(status, 200)
        self.assertIn("管理欄位", html)
        self.assertIn("管理金鑰", html)
        self.assertNotIn("已建立短網址", html)
        self.assertNotIn("https://u.kuies.tw/priv", html)
        self.assertNotIn("https://example.com/private", html)

    def test_external_manage_login_is_forbidden(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        body = b"token=secret"
        status, headers, response = app.handle("POST", "/surl/manage", {"content-type": "application/x-www-form-urlencoded"}, body, "8.8.8.8", "test")
        self.assertEqual(status, 403)
        self.assertNotIn("已建立短網址", response.decode())

    def test_internal_manage_login_shows_list_and_create_delete(self):
        self.store.create_url("https://example.com/delete", code="del")
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        login = b"token=secret"
        status, headers, response = app.handle("POST", "/surl/manage", {"content-type": "application/x-www-form-urlencoded"}, login, "192.168.1.50", "test")
        html = response.decode()
        self.assertEqual(status, 200)
        self.assertIn("已建立短網址", html)
        self.assertIn("https://u.kuies.tw/del", html)

        create_body = b"token=secret&url=https%3A%2F%2Fexample.com%2Fadmin&code=a1b2c&title=Admin"
        status, headers, response = app.handle("POST", "/surl/urls", {"content-type": "application/x-www-form-urlencoded"}, create_body, "192.168.1.50", "test")
        self.assertEqual(status, 200)
        self.assertEqual(self.store.lookup("a1b2c")["target_url"], "https://example.com/admin")

        delete_body = b"token=secret&code=del"
        status, headers, response = app.handle("POST", "/surl/delete", {"content-type": "application/x-www-form-urlencoded"}, delete_body, "192.168.1.50", "test")
        self.assertEqual(status, 200)
        self.assertIsNone(self.store.lookup("del"))

    def test_internal_manage_rejects_wrong_token(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        body = b"token=wrong"
        status, headers, response = app.handle("POST", "/surl/manage", {"content-type": "application/x-www-form-urlencoded"}, body, "192.168.1.50", "test")
        self.assertEqual(status, 401)
        self.assertNotIn("已建立短網址", response.decode())

    def test_total_count_alert_sends_once_when_reaching_1000(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret", alert_email="alerts@example.com", alert_from="short-url@example.com")
        sent = []
        app._send_alert_email = lambda subject, body: sent.append((subject, body)) or True
        for idx in range(999):
            self.store.create_url(f"https://example.com/pre/{idx}", code=f"pre{idx}")
        with self.store.connect() as conn:
            conn.execute("UPDATE urls SET created_at = 1, updated_at = 1")
        app._create_and_alert("https://example.com/threshold", code="hit1000")
        self.assertEqual(len(sent), 1)
        self.assertIn("總筆數已達 1000", sent[0][0])
        self.assertIn("目前總筆數：1000", sent[0][1])
        app._create_and_alert("https://example.com/after", code="hit1001")
        self.assertEqual(len(sent), 1)

    def test_recent_growth_alert_sends_when_100_urls_in_5_minutes(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret", alert_email="alerts@example.com", alert_from="short-url@example.com")
        sent = []
        app._send_alert_email = lambda subject, body: sent.append((subject, body)) or True
        for idx in range(99):
            self.store.create_url(f"https://recent-{idx}.example.com/recent/{idx}", code=f"rct{idx}")
        app._create_and_alert("https://recent-99.example.com/recent/99", code="rct99")
        self.assertEqual(len(sent), 1)
        self.assertIn("5 分鐘內新增 100 筆", sent[0][0])
        self.assertIn("5 分鐘內新增筆數：100", sent[0][1])
        app._create_and_alert("https://example.com/recent/100", code="rct100")
        self.assertEqual(len(sent), 1)

    def test_surl_page_uses_generic_tracking_label_and_antibot_note(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        status, headers, body = app.handle("GET", "/surl", {}, b"", "8.8.8.8", "test")
        html = body.decode()
        self.assertEqual(status, 200)
        self.assertIn("移除追蹤參數", html)
        self.assertIn("短時間內不可多次建立短網址", html)
        self.assertIn("追蹤清理目前支援 Threads、Facebook 與 IG", html)
        self.assertNotIn("移除 Threads.com / Threads.net 複製連結的 xmt 追蹤參數", html)

    def test_extension_popup_has_requested_layout_and_links(self):
        popup_path = os.path.join(os.path.dirname(__file__), "..", "extension", "threads-link-cleaner", "popup.html")
        with open(popup_path, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("自動去除並縮短", html)
        self.assertIn("🔄 更新狀態", html)
        self.assertIn("縮短網址+去除追蹤碼", html)
        self.assertIn("只縮短網址", html)
        self.assertIn("Threads、Facebook 與 IG", html)
        self.assertIn('<a href="https://u.kuies.tw/surl"', html)
        self.assertIn('<a href="https://www.threads.com/@kuies001"', html)

    def test_public_form_rejects_missing_antibot_token(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        body = b"url=https%3A%2F%2Fexample.com%2Fpublic&code=bot1"
        status, headers, response = app.handle("POST", "/shorten", {"content-type": "application/x-www-form-urlencoded"}, body, "8.8.8.8", "test")
        self.assertEqual(status, 400)
        self.assertIn("頁面驗證已過期", response.decode())

    def test_public_json_api_rate_limits_external_clients(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        last_status = None
        for idx in range(6):
            payload = json.dumps({"url": f"https://example.com/{idx}", "code": f"r{idx}"}).encode()
            last_status, headers, body = app.handle("POST", "/api/public/shorten", {"content-type": "application/json"}, payload, "8.8.8.8", "same-agent")
        self.assertEqual(last_status, 429)
        self.assertIn("短時間內使用次數過多", body.decode())

    def test_extension_fast_response_allows_repeated_copy_actions_without_falling_back_to_long_url(self):
        app = create_app(self.store, base_url="https://u.kuies.tw", admin_token="secret")
        statuses = []
        for idx in range(10):
            payload = json.dumps({
                "url": f"https://www.threads.com/@abc/post/repeated-{idx}?xmt=tracking",
                "clean_tracking": True,
                "fast_response": True,
            }).encode()
            status, headers, body = app.handle(
                "POST", "/api/public/shorten", {"content-type": "application/json"}, payload, "8.8.4.4", "extension-agent"
            )
            statuses.append(status)
            if status == 201:
                self.assertTrue(json.loads(body.decode())["short_url"].startswith("https://u.kuies.tw/"))
        self.assertEqual(statuses, [201] * 10)


if __name__ == "__main__":
    unittest.main()
