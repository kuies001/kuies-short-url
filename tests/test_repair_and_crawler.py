import contextlib
import importlib.util
import io
import json
import os
import tempfile
import time
import types
import unittest
from unittest import mock

import app as shortener_app
from app import ShortURLStore, create_app

ROOT = os.path.dirname(os.path.dirname(__file__))


def load_module(name, filename):
    path = os.path.join(ROOT, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_worker_module():
    return load_module("preview_refresh_worker_rc", "preview-refresh.py")


GOOD_METADATA = {
    "title": "真人貼文正文",
    "description": "貼文摘要文字",
    "image": "https://scontent.cdninstagram.com/v/t51/repaired.jpg",
}


class RepairPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "shorturls.sqlite3")
        self.store = ShortURLStore(self.db_path)
        self.original_preview = shortener_app.PREVIEW_IMAGE_DIR
        self.original_fallback = shortener_app.FALLBACK_PREVIEW_IMAGE_DIR
        shortener_app.PREVIEW_IMAGE_DIR = os.path.join(self.tmp.name, "images")
        shortener_app.FALLBACK_PREVIEW_IMAGE_DIR = os.path.join(self.tmp.name, "fallback-images")
        self.repair = load_module("repair_preview_module", "repair_preview.py")
        self.repair.DEFAULT_BACKUP_DIR = os.path.join(self.tmp.name, "backups")

    def tearDown(self):
        shortener_app.PREVIEW_IMAGE_DIR = self.original_preview
        shortener_app.FALLBACK_PREVIEW_IMAGE_DIR = self.original_fallback
        self.tmp.cleanup()

    def _fake_cache(self, code, image_url, timeout=8, force=False):
        image_dir = shortener_app.preview_image_dir()
        os.makedirs(image_dir, exist_ok=True)
        path = os.path.join(image_dir, f"{code}.jpg")
        with open(path, "wb") as fh:
            fh.write(b"image-bytes")
        return path, "image/jpeg"

    def test_public_metadata_accepts_good_and_rejects_degraded_variants(self):
        target = "https://www.threads.com/@a/post/RepairMeta1"
        self.assertEqual(
            self.repair.public_metadata(target, fetcher=lambda url: dict(GOOD_METADATA))["title"],
            GOOD_METADATA["title"],
        )
        self.assertIsNone(
            self.repair.public_metadata(target, fetcher=lambda url: {"title": "Threads 貼文", "description": "", "image": ""})
        )
        self.assertIsNone(
            self.repair.public_metadata(
                target,
                fetcher=lambda url: {"title": "Threads 貼文｜@a", "description": "個人頁", "image": "https://x/y.jpg"},
            )
        )
        self.assertIsNone(
            self.repair.public_metadata(target, fetcher=lambda url: {"title": "真人貼文", "description": "摘要", "image": ""})
        )

    def test_diagnose_verdicts(self):
        target = "https://www.threads.com/@a/post/RepairDiag1"
        public = self.repair.diagnose(target, fetcher=lambda url: dict(GOOD_METADATA), probe=lambda url: {"visibility": "public"})
        self.assertEqual(public["source"], "public")
        browser = self.repair.diagnose(target, fetcher=lambda url: {}, probe=lambda url: {"visibility": "public"})
        self.assertEqual(browser["source"], "browser")
        restricted = self.repair.diagnose(target, fetcher=lambda url: {}, probe=lambda url: {"visibility": "restricted"})
        self.assertEqual(restricted["source"], "")
        self.assertEqual(restricted["reason"], "gate-restricted")
        missing = self.repair.diagnose(target, fetcher=lambda url: {}, probe=lambda url: {"visibility": "missing"})
        self.assertEqual(missing["source"], "")
        self.assertEqual(missing["reason"], "gate-missing")

    def test_apply_public_metadata_upgrades_old_and_mints_new_code(self):
        target = "https://www.threads.com/@a/post/RepairApply1"
        self.store.create_url(target, code="oldpub")
        self.store.update_preview_metadata("oldpub", {"title": "Threads 貼文"}, "fallback")
        with mock.patch.object(self.repair, "cache_preview_image", side_effect=self._fake_cache):
            result = self.repair.apply_repair(
                self.store,
                "oldpub",
                {"source": "public", "metadata": dict(GOOD_METADATA), "visibility": "public", "reason": ""},
            )
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["new_code"])
        self.assertNotEqual(result["new_code"], "oldpub")
        old = self.store.lookup("oldpub")
        self.assertEqual(old["preview_status"], "ready")
        self.assertEqual(old["preview_title"], GOOD_METADATA["title"])
        self.assertEqual(old["preview_source"], "repair")
        new = self.store.lookup(result["new_code"])
        self.assertEqual(new["preview_status"], "ready")
        self.assertEqual(new["target_url"], target)
        self.assertTrue(os.path.isfile(result["backup"]))

    def test_apply_browser_path_requires_public_gate(self):
        target = "https://www.threads.com/@a/post/RepairApply2"
        self.store.create_url(target, code="brw1")
        self.store.update_preview_metadata("brw1", {"title": "Threads 貼文"}, "fallback")
        calls = {}

        def fake_browser_fetch(url, gate_verdict=None, use_budget=True):
            calls["url"] = url
            calls["gate"] = gate_verdict
            return {"ok": True, "restricted": False, "gate": "public", "error": "", "metadata": dict(GOOD_METADATA)}

        fake_module = types.SimpleNamespace(
            fetch_threads_preview=fake_browser_fetch,
            check_logged_out_visibility=lambda url: {"visibility": "public"},
        )
        with mock.patch.object(self.repair, "browser_preview", fake_module), mock.patch.object(
            self.repair, "cache_preview_image", side_effect=self._fake_cache
        ):
            result = self.repair.apply_repair(
                self.store, "brw1", {"source": "browser", "metadata": None, "visibility": "public", "reason": ""}
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(calls["gate"], "public")
        self.assertEqual(calls["url"], target)
        self.assertEqual(self.store.lookup("brw1")["preview_source"], "browser")

    def test_apply_browser_path_recheck_refuses_flaky_public(self):
        target = "https://www.threads.com/@a/post/RepairApply2b"
        self.store.create_url(target, code="brw2")
        self.store.update_preview_metadata("brw2", {"title": "Threads 貼文"}, "fallback")
        called = []
        fake_module = types.SimpleNamespace(
            fetch_threads_preview=lambda url, **kw: called.append(url) or {},
            check_logged_out_visibility=lambda url: {"visibility": "restricted"},
        )
        with mock.patch.object(self.repair, "browser_preview", fake_module):
            result = self.repair.apply_repair(
                self.store, "brw2", {"source": "browser", "metadata": None, "visibility": "public", "reason": ""}
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "gate-recheck-restricted")
        self.assertEqual(called, [])
        self.assertEqual(self.store.count_urls(), 1)

    def test_apply_refuses_non_repairable_and_keeps_old_code(self):
        target = "https://www.threads.com/@a/post/RepairApply3"
        self.store.create_url(target, code="rest1")
        self.store.update_preview_metadata("rest1", {"title": "Threads 貼文"}, "fallback")
        result = self.repair.apply_repair(
            self.store, "rest1", {"source": "", "metadata": None, "visibility": "restricted", "reason": "gate-restricted"}
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "gate-restricted")
        self.assertEqual(self.store.count_urls(), 1)
        self.assertEqual(self.store.lookup("rest1")["preview_status"], "fallback")

    def test_apply_image_failure_leaves_old_code_unchanged(self):
        target = "https://www.threads.com/@a/post/RepairApply4"
        self.store.create_url(target, code="imgfail")
        self.store.update_preview_metadata("imgfail", {"title": "Threads 貼文"}, "fallback")
        with mock.patch.object(self.repair, "cache_preview_image", return_value=(None, None)):
            result = self.repair.apply_repair(
                self.store, "imgfail", {"source": "public", "metadata": dict(GOOD_METADATA), "visibility": "public", "reason": ""}
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "image-download-failed")
        self.assertEqual(self.store.lookup("imgfail")["preview_status"], "fallback")
        self.assertEqual(self.store.count_urls(), 1)

    def test_cli_repair_dry_run_does_not_write(self):
        target = "https://www.threads.com/@a/post/RepairDry1"
        self.store.create_url(target, code="dryone")
        self.store.update_preview_metadata("dryone", {"title": "Threads 貼文"}, "fallback")
        self.repair.diagnose = lambda url, **kw: {
            "source": "public",
            "metadata": dict(GOOD_METADATA),
            "visibility": "public",
            "reason": "",
        }
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.repair.cmd_repair(self.store, "dryone", apply=False)
        self.assertEqual(code, 0)
        self.assertIn("dry-run", stdout.getvalue())
        self.assertEqual(self.store.lookup("dryone")["preview_status"], "fallback")
        self.assertEqual(self.store.count_urls(), 1)

    def test_cli_refuses_restricted_post(self):
        target = "https://www.threads.com/@a/post/RepairRestricted1"
        self.store.create_url(target, code="restcli")
        self.store.update_preview_metadata("restcli", {"title": "Threads 貼文"}, "fallback")
        self.repair.diagnose = lambda url, **kw: {
            "source": "",
            "metadata": None,
            "visibility": "restricted",
            "reason": "gate-restricted",
        }
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.repair.cmd_repair(self.store, "restcli", apply=True)
        self.assertEqual(code, 1)
        self.assertIn("不補救", stdout.getvalue())
        self.assertEqual(self.store.count_urls(), 1)
        self.assertEqual(self.store.lookup("restcli")["preview_status"], "fallback")

    def test_collect_degraded_lists_fallback_rows(self):
        self.store.create_url("https://www.threads.com/@a/post/Collect1", code="col1")
        self.store.update_preview_metadata("col1", {"title": "Threads 貼文"}, "fallback")
        rows = self.repair.collect_degraded(self.store, limit=10)
        self.assertIn("col1", [row["code"] for row in rows])


class CrawlerViewCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "shorturls.sqlite3")
        self.store = ShortURLStore(self.db_path)
        self.worker = load_worker_module()
        self.state_path = os.path.join(self.tmp.name, "crawler-state.json")
        self.log_path = os.path.join(self.tmp.name, "crawler-health.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def _add(self, code, status="ready", created=None):
        self.store.create_url(f"https://www.threads.com/@a/post/{code}", code=code)
        now = int(time.time())
        with self.store.connect() as conn:
            conn.execute("UPDATE urls SET preview_status=?, created_at=? WHERE code=?", (status, created or now, code))

    def _run(self, fetcher):
        return self.worker.crawler_view_check(
            self.db_path,
            now=int(time.time()),
            fetcher=fetcher,
            image_probe=lambda url: True,
            state_path=self.state_path,
            log_path=self.log_path,
        )

    def test_stable_ok_is_silent_and_changes_are_reported(self):
        self._add("cw1")
        good = lambda code: {
            "status": 200,
            "title": "真人貼文正文",
            "description": "摘要",
            "image": "https://u.kuies.tw/preview-image/cw1.jpg",
        }
        self.assertEqual(self._run(good)["reported"], [])
        self.assertEqual(self._run(good)["reported"], [])
        bad = lambda code: {"status": 200, "title": "Threads 貼文", "description": "", "image": ""}
        third = self._run(bad)
        self.assertEqual(len(third["reported"]), 1)
        self.assertEqual(third["reported"][0]["verdict"], "degraded")
        self.assertEqual(third["reported"][0]["previous"], "ok")
        fourth = self._run(good)
        self.assertEqual(len(fourth["reported"]), 1)
        self.assertEqual(fourth["reported"][0]["verdict"], "ok")

    def test_first_sighting_of_broken_ready_card_is_reported(self):
        self._add("cw2")
        bad = lambda code: {"status": 200, "title": "Threads 貼文", "description": "", "image": ""}
        result = self._run(bad)
        self.assertEqual(len(result["reported"]), 1)
        self.assertIsNone(result["reported"][0]["previous"])

    def test_first_sighting_acceptable_profile_card_is_silent(self):
        self._add("cw3", status="profile_fallback")
        profile = lambda code: {
            "status": 200,
            "title": "Threads 貼文｜@a",
            "description": "個人頁",
            "image": "https://u.kuies.tw/preview-image/cw3.jpg",
        }
        self.assertEqual(self._run(profile)["reported"], [])

    def test_offline_outage_aggregates_without_per_code_spam(self):
        for code in ("cw4", "cw5", "cw6"):
            self._add(code)
        down = lambda code: {"status": 0, "title": "", "description": "", "image": ""}
        result = self._run(down)
        self.assertEqual(result["offline_errors"], 3)
        self.assertEqual(result["reported"], [])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.worker._print_crawler_report(result)
        self.assertIn("無法連線", stdout.getvalue())

    def test_retryable_verdict_is_flagged_for_ready_cards(self):
        self._add("cw7")
        retry = lambda code: {"status": 503, "title": "", "description": "", "image": ""}
        result = self._run(retry)
        self.assertEqual(len(result["reported"]), 1)
        self.assertEqual(result["reported"][0]["verdict"], "retryable")
        self.assertEqual(result["offline_errors"], 0)

    def test_state_file_is_written_for_next_run(self):
        self._add("cw8")
        good = lambda code: {
            "status": 200,
            "title": "真人貼文正文",
            "description": "摘要",
            "image": "https://u.kuies.tw/preview-image/cw8.jpg",
        }
        self._run(good)
        with open(self.state_path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        self.assertEqual(state["verdicts"]["cw8"], "ok")
        with open(self.log_path, "r", encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(lines[0]["code"], "cw8")

    def test_analyze_crawler_retries_detects_retry_after_503(self):
        retry_log = os.path.join(self.tmp.name, "crawler-views.jsonl")
        entries = [
            {"ts": 1, "code": "r1", "status": 503, "ua": "meta"},
            {"ts": 2, "code": "r1", "status": 200, "ua": "meta"},
            {"ts": 3, "code": "r2", "status": 503, "ua": "meta"},
            {"ts": 4, "code": "r3", "status": 200, "ua": "meta"},
            {"ts": 5, "code": "r1", "status": 200, "ua": "health"},
            {"ts": 6, "code": "r1", "status": 503, "ua": "meta", "ip": "lan"},
        ]
        with open(retry_log, "w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")
        result = self.worker.analyze_crawler_retries(retry_log)
        self.assertEqual(result["entries"], 6)
        self.assertEqual(result["meta_hits"], 4)
        self.assertEqual(result["local_ignored"], 1)
        self.assertEqual(result["retryable_codes"], 2)
        self.assertEqual(result["retried_codes"], 1)
        self.assertEqual(result["samples"][0]["code"], "r1")
        self.assertEqual(result["samples"][0]["after"], [200])
        self.assertEqual(result["samples"][0]["gap_seconds"], 1)

    def test_analyze_crawler_retries_empty_log(self):
        result = self.worker.analyze_crawler_retries(os.path.join(self.tmp.name, "missing.jsonl"))
        self.assertEqual(result["entries"], 0)
        self.assertEqual(result["retryable_codes"], 0)


class CrawlerLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "shorturls.sqlite3")
        self.store = ShortURLStore(self.db_path)
        self.log_path = os.path.join(self.tmp.name, "crawler-views.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_service_logs_crawler_views_for_social_targets_with_ua_class(self):
        self.store.create_url("https://www.threads.com/@a/post/log1", code="log1")
        self.store.create_url("https://example.com/plain", code="plain1")
        app = create_app(
            self.store,
            "https://u.kuies.tw",
            "secret",
            preview_warm_enabled=True,
            crawler_log_path=self.log_path,
        )
        app.preview_crawler_wait_seconds = 0.05
        original_fetch = shortener_app.fetch_open_graph_metadata
        original_profile = shortener_app.fetch_social_profile_fallback_metadata
        shortener_app.fetch_open_graph_metadata = lambda url: {}
        shortener_app.fetch_social_profile_fallback_metadata = lambda url: {}
        try:
            meta_ua = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
            app.handle("GET", "/log1", {}, b"", "8.8.8.8", meta_ua)
            app.handle("GET", "/log1", {}, b"", "8.8.8.8", meta_ua + " kuies-preview-health/1.0")
            app.handle("GET", "/log1", {}, b"", "192.168.1.22", meta_ua)
            app.handle("GET", "/plain1", {}, b"", "8.8.8.8", meta_ua)
        finally:
            shortener_app.fetch_open_graph_metadata = original_fetch
            shortener_app.fetch_social_profile_fallback_metadata = original_profile
        with open(self.log_path, "r", encoding="utf-8") as fh:
            entries = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0]["code"], "log1")
        self.assertEqual(entries[0]["ua"], "meta")
        self.assertEqual(entries[0]["status"], 503)
        self.assertEqual(entries[0]["ip"], "public")
        self.assertEqual(entries[1]["ua"], "health")
        self.assertEqual(entries[2]["ua"], "meta")
        self.assertEqual(entries[2]["ip"], "lan")


class BrowserPreviewGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bp = load_module("browser_preview_module", "browser_preview.py")
        self.bp.ATTEMPT_STATE_PATH = os.path.join(self.tmp.name, "attempts.json")
        self.bp.FETCH_LOCK_PATH = os.path.join(self.tmp.name, "fetch.lock")

    def tearDown(self):
        self.tmp.cleanup()

    def test_non_public_verdicts_are_refused_without_logged_in_fetch(self):
        target = "https://www.threads.com/@abc/post/AbCdEf123"
        called = []
        original = self.bp.fetch_threads_post_metadata
        self.bp.fetch_threads_post_metadata = lambda url: called.append(url) or {}
        try:
            for verdict in ("restricted", "missing", "login-wall", "unknown"):
                result = self.bp.fetch_threads_preview(target, use_budget=False, gate_verdict=verdict)
                self.assertFalse(result["ok"])
                self.assertEqual(result["gate"], verdict)
                self.assertEqual(result["error"], f"gate-{verdict}")
        finally:
            self.bp.fetch_threads_post_metadata = original
        self.assertEqual(called, [])

    def test_public_verdict_allows_logged_in_fetch(self):
        target = "https://www.threads.com/@abc/post/AbCdEf123"
        original = self.bp.fetch_threads_post_metadata
        self.bp.fetch_threads_post_metadata = lambda url: {
            "ok": True,
            "restricted": False,
            "error": "",
            "metadata": {"title": "x", "description": "y", "image": "https://scontent.cdninstagram.com/v/z.jpg"},
        }
        try:
            result = self.bp.fetch_threads_preview(target, use_budget=False, gate_verdict="public")
        finally:
            self.bp.fetch_threads_post_metadata = original
        self.assertTrue(result["ok"])
        self.assertEqual(result["gate"], "public")

    def test_parse_browser_result_guardrails(self):
        target = "https://www.threads.com/@abc/post/AbCdEf123"
        restricted = json.dumps(
            {
                "status": "ok",
                "data": {
                    "href": target,
                    "ogTitle": "",
                    "ogDescription": "",
                    "ogImage": "",
                    "restricted": True,
                    "noAccess": False,
                    "missing": False,
                },
            }
        )
        parsed = self.bp.parse_browser_result(restricted, target_url=target)
        self.assertTrue(parsed["restricted"])
        self.assertFalse(parsed["ok"])
        redirected = json.dumps(
            {
                "status": "ok",
                "data": {
                    "href": "https://www.threads.com/@a",
                    "ogTitle": "Threads",
                    "ogDescription": "",
                    "ogImage": "",
                    "restricted": False,
                    "noAccess": False,
                    "missing": False,
                },
            }
        )
        parsed2 = self.bp.parse_browser_result(redirected, target_url=target)
        self.assertEqual(parsed2["error"], "redirected-away")
        good = json.dumps(
            {
                "status": "ok",
                "data": {
                    "href": target,
                    "ogTitle": "作者",
                    "ogDescription": "貼文內容",
                    "ogImage": "https://scontent.cdninstagram.com/v/a.jpg",
                    "restricted": False,
                    "noAccess": False,
                    "missing": False,
                },
            }
        )
        parsed3 = self.bp.parse_browser_result(good, target_url=target)
        self.assertTrue(parsed3["ok"])
        self.assertEqual(parsed3["metadata"]["description"], "貼文內容")

    def test_whitelist_accepts_only_threads_post_permalinks(self):
        self.assertTrue(self.bp.is_browser_preview_target("https://www.threads.com/@a.b_c/post/AbCdEf123"))
        self.assertFalse(self.bp.is_browser_preview_target("https://www.threads.com/@a"))
        self.assertFalse(self.bp.is_browser_preview_target("http://www.threads.com/@a/post/AbCdEf123"))
        self.assertFalse(self.bp.is_browser_preview_target("https://evil.com/@a/post/AbCdEf123"))


class CrawlerViewModuleTests(unittest.TestCase):
    def setUp(self):
        self.cv = load_module("crawler_view_module", "crawler_view.py")

    def test_classify_view_buckets(self):
        self.assertEqual(self.cv.classify_view({"status": 503}), "retryable")
        self.assertEqual(self.cv.classify_view({"status": 500, "title": "x"}), "error")
        self.assertEqual(
            self.cv.classify_view({"status": 200, "title": "Threads 貼文", "image": "https://x/y.jpg"}), "degraded"
        )
        self.assertEqual(
            self.cv.classify_view({"status": 200, "title": "Threads 貼文｜@a", "image": "https://x/y.jpg"}), "profile"
        )
        self.assertEqual(
            self.cv.classify_view(
                {"status": 200, "title": "真人貼文", "image": "https://x/y.jpg"}, image_probe=lambda url: True
            ),
            "ok",
        )
        self.assertEqual(
            self.cv.classify_view(
                {"status": 200, "title": "真人貼文", "image": "https://x/y.jpg"}, image_probe=lambda url: False
            ),
            "degraded",
        )


if __name__ == "__main__":
    unittest.main()
