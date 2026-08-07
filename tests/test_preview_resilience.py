import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest

import app as shortener_app
from app import ShortURLStore, create_app


ROOT = os.path.dirname(os.path.dirname(__file__))


def load_worker_module():
    path = os.path.join(ROOT, "preview-refresh.py")
    spec = importlib.util.spec_from_file_location("preview_refresh_worker", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PreviewResilienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "shorturls.sqlite3")
        self.store = ShortURLStore(self.db_path)
        self.original_preview = shortener_app.PREVIEW_IMAGE_DIR
        self.original_fallback = shortener_app.FALLBACK_PREVIEW_IMAGE_DIR
        shortener_app.PREVIEW_IMAGE_DIR = os.path.join(self.tmp.name, "images")
        shortener_app.FALLBACK_PREVIEW_IMAGE_DIR = os.path.join(self.tmp.name, "fallback-images")

    def tearDown(self):
        shortener_app.PREVIEW_IMAGE_DIR = self.original_preview
        shortener_app.FALLBACK_PREVIEW_IMAGE_DIR = self.original_fallback
        self.tmp.cleanup()

    def test_schema_migrates_old_database_without_losing_rows(self):
        old_db = os.path.join(self.tmp.name, "old.sqlite3")
        with sqlite3.connect(old_db) as conn:
            conn.execute("CREATE TABLE urls(code TEXT PRIMARY KEY, target_url TEXT NOT NULL, title TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, clicks INTEGER NOT NULL DEFAULT 0, last_clicked_at INTEGER)")
            conn.execute("INSERT INTO urls VALUES ('old1', 'https://www.threads.com/@a/post/1', NULL, 1, 1, 0, NULL)")
        migrated = ShortURLStore(old_db)
        row = migrated.lookup("old1")
        self.assertIsNotNone(row)
        for column in ("preview_failure_count", "preview_next_retry_at", "preview_last_error", "preview_version"):
            self.assertIn(column, row)
        self.assertEqual(row["preview_failure_count"], 0)

    def test_fallback_uses_bounded_exponential_backoff_and_ready_resets_it(self):
        self.store.create_url("https://www.threads.com/@a/post/1", code="retry")
        now = int(time.time())
        self.store.update_preview_metadata("retry", {}, "fallback")
        first = self.store.lookup("retry")
        self.assertEqual(first["preview_failure_count"], 1)
        self.assertGreaterEqual(first["preview_next_retry_at"], now + 50 * 60)
        with self.store.connect() as conn:
            conn.execute("UPDATE urls SET preview_failure_count = 20 WHERE code = 'retry'")
        self.store.update_preview_metadata("retry", {}, "fallback")
        capped = self.store.lookup("retry")
        self.assertLessEqual(capped["preview_next_retry_at"] - int(time.time()), 7 * 24 * 60 * 60)
        self.store.update_preview_metadata("retry", {"title": "成功"}, "ready")
        ready = self.store.lookup("retry")
        self.assertEqual(ready["preview_failure_count"], 0)
        self.assertIsNone(ready["preview_next_retry_at"])
        self.assertIsNone(ready["preview_last_error"])

    def test_preview_metadata_or_image_change_changes_versioned_url(self):
        self.store.create_url("https://www.threads.com/@a/post/2", code="version")
        image_dir = shortener_app.preview_image_dir()
        os.makedirs(image_dir, exist_ok=True)
        image_path = os.path.join(image_dir, "version.jpg")
        with open(image_path, "wb") as fh:
            fh.write(b"one")
        self.store.update_preview_metadata("version", {"title": "一"}, "ready", image_path=image_path)
        app = create_app(self.store, "https://u.kuies.tw", "secret", preview_warm_enabled=True)
        first = app._preview_image_url(self.store.lookup("version"))
        self.store.update_preview_metadata("version", {"title": "二"}, "ready", image_path=image_path)
        second = app._preview_image_url(self.store.lookup("version"))
        with open(image_path, "wb") as fh:
            fh.write(b"two")
        self.store.update_preview_metadata("version", {"title": "二"}, "ready", image_path=image_path)
        third = app._preview_image_url(self.store.lookup("version"))
        self.assertRegex(first, r"/preview-image/version-[0-9a-f]{12}\.jpg$")
        self.assertNotEqual(first, second)
        self.assertNotEqual(second, third)

    def test_versioned_and_legacy_image_routes_work_and_reject_unsafe_names(self):
        self.store.create_url("https://www.threads.com/@a/post/3", code="safe-code")
        image_dir = shortener_app.preview_image_dir()
        os.makedirs(image_dir, exist_ok=True)
        image_path = os.path.join(image_dir, "safe-code.jpg")
        with open(image_path, "wb") as fh:
            fh.write(b"image")
        self.store.update_preview_metadata("safe-code", {"title": "卡片"}, "ready", image_path=image_path)
        app = create_app(self.store, "https://u.kuies.tw", "secret", preview_warm_enabled=True)
        versioned = app._preview_image_url(self.store.lookup("safe-code"))
        for path in (versioned.removeprefix("https://u.kuies.tw"), "/preview-image/safe-code.jpg"):
            status, _, body = app.handle("GET", path, {}, b"", "8.8.8.8", "test")
            self.assertEqual(status, 200)
            self.assertEqual(body, b"image")
        for path in ("/preview-image/../safe-code.jpg", "/preview-image/%2Fetc.jpg", "/preview-image/safe-code-deadbeef0000.exe"):
            status, _, _ = app.handle("GET", path, {}, b"", "8.8.8.8", "test")
            self.assertEqual(status, 404)

    def test_preview_status_api_is_read_only_cors_safe_and_reports_image(self):
        self.store.create_url("https://www.instagram.com/p/ABC/", code="state")
        image_dir = shortener_app.preview_image_dir()
        os.makedirs(image_dir, exist_ok=True)
        image_path = os.path.join(image_dir, "state.jpg")
        with open(image_path, "wb") as fh:
            fh.write(b"image")
        self.store.update_preview_metadata("state", {"title": "IG"}, "profile_fallback", image_path=image_path)
        app = create_app(self.store, "https://u.kuies.tw", "secret", preview_warm_enabled=True)
        before = self.store.lookup("state")
        status, headers, body = app.handle("GET", "/api/public/preview-status/state", {}, b"", "8.8.8.8", "extension")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(data["preview_status"], "profile_fallback")
        self.assertTrue(data["preview_available"])
        self.assertIn("/preview-image/state-", data["preview_image_url"])
        self.assertEqual(before, self.store.lookup("state"))
        bad_status, _, _ = app.handle("GET", "/api/public/preview-status/%2Fetc", {}, b"", "8.8.8.8", "extension")
        self.assertEqual(bad_status, 404)

    def test_fast_response_includes_pending_preview_status(self):
        app = create_app(self.store, "https://u.kuies.tw", "secret", preview_warm_enabled=False)
        payload = json.dumps({"url": "https://www.threads.com/@a/post/fast", "fast_response": True}).encode()
        status, _, body = app.handle("POST", "/api/public/shorten", {"content-type": "application/json"}, payload, "8.8.8.8", "extension")
        data = json.loads(body)
        self.assertEqual(status, 201)
        self.assertEqual(data["preview_status"], "pending")
        self.assertFalse(data["preview_available"])

    def test_worker_priority_batch_dry_run_and_silent_success(self):
        worker = load_worker_module()
        now = int(time.time())
        rows = [
            ("ready-old", "https://www.threads.com/@a/post/ready", "ready", now - 8 * 86400, None),
            ("fallback-due", "https://www.instagram.com/p/due/", "fallback", now - 100, now - 1),
            ("pending", "https://www.threads.com/@a/post/pending", None, None, None),
            ("fallback-later", "https://www.instagram.com/p/later/", "fallback", now - 100, now + 3600),
            ("other", "https://example.com/no", None, None, None),
            ("lookalike", "https://evilthreads.com/@a/post/no", None, None, None),
        ]
        for code, url, status, updated, retry in rows:
            self.store.create_url(url, code=code)
            with self.store.connect() as conn:
                conn.execute("UPDATE urls SET preview_status=?, preview_updated_at=?, preview_next_retry_at=? WHERE code=?", (status, updated, retry, code))
        selected = self.store.preview_refresh_candidates(limit=2, now=now)
        self.assertEqual([row["code"] for row in selected], ["pending", "fallback-due"])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = worker.run_worker(self.db_path, limit=2, dry_run=True, delay=0)
        self.assertEqual(result["selected"], 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIsNone(self.store.lookup("pending")["preview_updated_at"])
        self.assertNotIn("lookalike", [row["code"] for row in self.store.preview_refresh_candidates(limit=20, now=now)])

    def test_worker_isolates_item_failure_and_health_check_only_warns_over_threshold(self):
        worker = load_worker_module()
        self.store.create_url("https://www.threads.com/@a/post/fail", code="fail")
        original = shortener_app.ShortURLApp._warm_preview
        try:
            shortener_app.ShortURLApp._warm_preview = lambda app, row, **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = worker.run_worker(self.db_path, limit=20, delay=0)
        finally:
            shortener_app.ShortURLApp._warm_preview = original
        self.assertEqual(result["failed"], 1)
        self.assertIn("預覽刷新異常", stdout.getvalue())
        failed = self.store.lookup("fail")
        self.assertEqual(failed["preview_failure_count"], 1)
        self.assertIn("RuntimeError", failed["preview_last_error"])
        empty_db = os.path.join(self.tmp.name, "healthy.sqlite3")
        ShortURLStore(empty_db)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            health = worker.run_health_check(empty_db)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(health["total_social"], 0)

        pending_db = os.path.join(self.tmp.name, "pending.sqlite3")
        pending_store = ShortURLStore(pending_db)
        pending_store.create_url("https://www.threads.com/@a/post/new", code="new")
        pending_health = worker.health_statistics(pending_db)
        self.assertEqual(pending_health["pending"], 1)
        self.assertEqual(pending_health["missing_images"], 0)

    def test_extension_waits_for_preview_and_timeout_still_returns_short_url(self):
        script = os.path.join(ROOT, "tests", "test_content_preview_wait.js")
        result = subprocess.run(["node", script], capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, msg=f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
        self.assertIn("preview-wait-and-short-url-timeout: ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
