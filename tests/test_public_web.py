import json
import http.client
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from trace_review.core import make_run
from trace_review.public_access import PublicLimits, public_origin
from web_app import Server, Workspace


class PublicWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.server = Server(("127.0.0.1", 0), cls.folder.name, origin="https://review.njfu.org")
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(3)
        cls.folder.cleanup()

    def setUp(self):
        self.cookie = ""
        self.last_headers = None
        self.client_ip = str(time.monotonic_ns())

    def call(self, path, payload=None, thread="main", headers=None, fresh=False):
        fields = {"Host":"review.njfu.org", "X-Trace-Conversation":thread,
                  "CF-Connecting-IP":self.client_ip}
        if self.cookie and not fresh:
            fields["Cookie"] = self.cookie
        if payload is not None:
            fields.update({"X-Trace-Request":"1", "Origin":"https://review.njfu.org"})
        fields.update(headers or {})
        req = urllib.request.Request(self.base + path, headers=fields,
                                     data=json.dumps(payload).encode() if payload is not None else None)
        with urllib.request.urlopen(req, timeout=3) as response:
            self.last_headers = response.headers
            cookie = response.headers.get("Set-Cookie")
            if cookie and not fresh:
                self.cookie = cookie.split(";", 1)[0]
            return response.status, json.load(response)

    def settled(self):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            data = self.call("/api/state")[1]
            if data["progress"]["status"] != "running":
                return data
            time.sleep(.02)
        self.fail("background operation did not finish")

    def raw_request(self, method, path, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request(method, path, headers={"Host":"review.njfu.org", **(headers or {})})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_http_public_entry_redirects_before_starting_a_workspace(self):
        for method in ("GET", "HEAD"):
            status, headers, body = self.raw_request(method, "/?from=edge", {"X-Forwarded-Proto":"http"})
            self.assertEqual(status, 308)
            self.assertEqual(headers["Location"], "https://review.njfu.org/?from=edge")
            self.assertEqual(body, b"")
            self.assertNotIn("Set-Cookie", headers)
        status, headers, _ = self.raw_request("GET", "//external.invalid/", {"X-Forwarded-Proto":"http"})
        self.assertEqual(status, 308)
        self.assertTrue(headers["Location"].startswith("https://review.njfu.org/"))

    def test_https_entry_and_local_preview_remain_accessible(self):
        status, headers, body = self.raw_request("GET", "/", {"X-Forwarded-Proto":"https"})
        self.assertEqual(status, 200)
        self.assertIn(b"studioShell", body)
        self.assertEqual(headers["Strict-Transport-Security"], "max-age=31536000")
        status, headers, body = self.raw_request("HEAD", "/", {"X-Forwarded-Proto":"https"})
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertGreater(int(headers["Content-Length"]), 0)
        status, _, _ = self.raw_request("GET", "/", {"Host":f"127.0.0.1:{self.server.server_address[1]}", "X-Forwarded-Proto":"http"})
        self.assertEqual(status, 200)

    def test_plain_http_mutations_are_not_processed_or_redirected(self):
        with patch("web_app.make_run") as generate:
            status, headers, body = self.raw_request("POST", "/api/generate", {"X-Forwarded-Proto":"http"})
        self.assertEqual(status, 426)
        self.assertIn("HTTPS", json.loads(body)["error"])
        self.assertNotIn("Location", headers)
        self.assertNotIn("Set-Cookie", headers)
        generate.assert_not_called()

    def test_public_visitors_never_load_owner_credentials(self):
        with patch("web_app.load_model_config", side_effect=AssertionError("owner settings accessed")):
            state = self.call("/api/state")[1]
            self.assertFalse(state["config"]["key_set"])
            self.assertTrue(state["config"]["visitor_key"])
            self.assertIn("Secure", self.last_headers["Set-Cookie"])
            self.call("/api/threads/new", {})
            folder = Path(self.folder.name) / self.cookie.split("=", 1)[1]
            restored = Workspace(folder, public=True)
            self.assertTrue(all(not s.config["key"] for s in restored.threads.values()))

    def test_visitor_key_is_private_and_never_persisted(self):
        self.call("/api/state")
        with patch("web_app.save_model_config") as save:
            data = self.call("/api/config", {"key":"visitor-test-secret", "persist":True})[1]
            save.assert_not_called()
        self.assertTrue(data["config"]["key_set"])
        self.assertNotIn("visitor-test-secret", json.dumps(data))
        other = self.call("/api/state", fresh=True)[1]
        self.assertFalse(other["config"]["key_set"])
        sibling = self.call("/api/threads/new", {})[1]
        self.assertTrue(sibling["config"]["key_set"])
        for file in Path(self.folder.name).rglob("*.json"):
            self.assertNotIn("visitor-test-secret", file.read_text("utf-8"))
        self.assertFalse(self.call("/api/config", {"clear_key":True})[1]["config"]["key_set"])

    def test_arbitrary_provider_addresses_and_wrong_origins_rejected(self):
        self.call("/api/state")
        for base in ("http://127.0.0.1:8765", "https://other.invalid", "https://api.deepseek.com.evil.invalid", "https://api.deepseek.com@evil.invalid"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.call("/api/config", {"base":base,"key":"test"})
            self.assertEqual(error.exception.code, 400)
        for headers in ({"Host":"evil.invalid"}, {"Origin":"https://evil.invalid"},
                        {"Origin":"http://review.njfu.org"}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.call("/api/demo", {}, headers=headers)
            self.assertEqual(error.exception.code, 403)

    def test_public_generation_returns_before_work_finishes_and_commits(self):
        before = self.call("/api/state")[1]
        started, release = threading.Event(), threading.Event()
        def slow(corpus, topic, mode, **kwargs):
            kwargs["progress"]("generation", "正在写作")
            started.set()
            release.wait(3)
            run = make_run(corpus, "反馈", "extract")
            run["topic"] = topic
            return run
        with patch("web_app.make_run", side_effect=slow):
            try:
                code, accepted = self.call("/api/generate", {"topic":"公开站测试", "mode":"extract", "operation_id":"job-test"})
                self.assertEqual(code, 202)
                self.assertEqual(accepted["progress"]["operation_id"], "job-test")
                self.assertTrue(started.wait(1))
                pending = self.call("/api/state")[1]
                self.assertEqual(pending["run"], before["run"])
                self.assertEqual(pending["progress"]["status"], "running")
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.call("/api/generate", {"topic":"重复请求","mode":"extract"})
                self.assertEqual(error.exception.code, 409)
            finally:
                release.set()
            done = self.settled()
        self.assertEqual(done["progress"]["status"], "complete")
        self.assertEqual(done["run"]["topic"], "公开站测试")
        self.assertTrue(self.call("/api/threads/new", {})[1]["thread_id"])

    def test_missing_key_fails_without_owner_fallback_and_preserves_draft(self):
        original = self.call("/api/state")[1]["run"]
        with patch("web_app.make_run") as generate:
            self.call("/api/generate", {"topic":"测试", "mode":"model"})
            done = self.settled()
            generate.assert_not_called()
        self.assertEqual(done["progress"]["status"], "error")
        self.assertIn("自己的 DeepSeek", done["progress"]["events"][-1]["message"])
        self.assertEqual(done["run"], original)
        self.assertTrue(self.call("/api/config", {"key":"fake-key"})[1]["config"]["key_set"])

    def test_background_error_releases_capacity_and_keeps_current_draft(self):
        original = self.call("/api/state")[1]["run"]
        with patch("web_app.make_run", side_effect=ValueError("测试生成失败")):
            self.call("/api/generate", {"topic":"测试", "mode":"extract"})
            done = self.settled()
        self.assertEqual(done["run"], original)
        self.assertEqual(done["progress"]["status"], "error")
        self.assertTrue(self.server.limits.jobs.acquire(blocking=False))
        self.assertTrue(self.server.limits.jobs.acquire(blocking=False))
        self.assertFalse(self.server.limits.jobs.acquire(blocking=False))
        self.server.limits.jobs.release()
        self.server.limits.jobs.release()


class PublicBoundsTests(unittest.TestCase):
    def test_origin_must_be_an_https_origin(self):
        for value in ("http://review.njfu.org", "https://user:pass@example.com", "https://example.com/path", "https://example.com?x=1"):
            with self.assertRaises(ValueError):
                public_origin(value)
        self.assertEqual(public_origin("https://review.njfu.org/"), "https://review.njfu.org")

    def test_rate_limit_recovers_after_window(self):
        limits = PublicLimits()
        with patch("trace_review.public_access.time.monotonic", return_value=100):
            self.assertTrue(limits.allow("client", "write", 2, 60))
            self.assertTrue(limits.allow("client", "write", 2, 60))
            self.assertFalse(limits.allow("client", "write", 2, 60))
            self.assertTrue(limits.allow("another", "write", 2, 60))
        with patch("trace_review.public_access.time.monotonic", return_value=161):
            self.assertTrue(limits.allow("client", "write", 2, 60))


if __name__ == "__main__":
    unittest.main()
