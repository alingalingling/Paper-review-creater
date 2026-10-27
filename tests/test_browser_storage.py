import base64
import io
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

from tests import test_public_web
from trace_review.browser_jobs import BrowserJobs
from web_app import Session


class BrowserStorageTests(unittest.TestCase):
    setUpClass = classmethod(test_public_web.PublicWebTests.setUpClass.__func__)
    tearDownClass = classmethod(test_public_web.PublicWebTests.tearDownClass.__func__)
    call = test_public_web.PublicWebTests.call

    def setUp(self):
        test_public_web.PublicWebTests.setUp(self)
        self.sample = self.call("/api/bootstrap")[1]["sample"]

    def task(self, operation, snapshot, body=None, config=None, ack=True):
        status, accepted = self.call("/api/browser/execute", {
            "operation":operation, "snapshot":snapshot, "body":body or {}, "config":config or {}})
        self.assertEqual(status, 202)
        identifier = accepted["job_id"]
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            job = self.call("/api/browser/jobs/" + identifier)[1]
            if job["ready"]:
                if ack:
                    self.call("/api/browser/ack", {"job_id":identifier})
                return job, identifier
            time.sleep(.01)
        self.fail("task did not complete")

    def test_bootstrap_never_allocates_a_workspace_or_owner_key(self):
        with patch("web_app.load_model_config", side_effect=AssertionError("owner credentials")):
            status, data = self.call("/api/bootstrap", fresh=True)
        self.assertEqual(status, 200)
        self.assertTrue(data["browser_storage"])
        self.assertFalse(data["sample"]["config"]["key_set"])
        self.assertEqual(self.server.sessions, {})

    def test_import_generate_annotate_with_browser_snapshots_without_disk_writes(self):
        text = "本合成材料比较反馈方式，用于验证浏览器存储，不构成真实研究结论。"
        imported, _ = self.task("/api/import", {}, {"files":[{
            "name":"存储测试.txt", "data":base64.b64encode(text.encode()).decode()}]})
        self.assertEqual(imported["code"], 200)
        self.assertEqual(len(imported["result"]["corpus"]["documents"]), 1)
        generated, _ = self.task("/api/generate", imported["result"], {"mode":"extract", "topic":"反馈方式"})
        self.assertEqual(generated["code"], 200)
        self.assertTrue(generated["result"]["run"]["claims"])
        claim = generated["result"]["run"]["claims"][0]
        annotated, _ = self.task("/api/annotate", generated["result"], {
            "id":claim["id"], "label":"supported", "reviewer":"test", "note":"已核对"})
        self.assertEqual(annotated["result"]["metrics"]["supported"], 1)
        self.assertEqual(self.server.sessions, {})
        self.assertFalse(list(Path(self.folder.name).rglob("*.json")))
        self.assertEqual(self.server.browser_jobs.items, {})

    def test_browser_job_can_generate_chinese_review_from_english_upload(self):
        text="Synthetic example: soil and water conservation is described in this document."
        imported,_=self.task("/api/import",{}, {"files":[{
            "name":"conservation.txt","data":base64.b64encode(text.encode()).decode()}]})
        def generate(topic,candidates,**kwargs):
            self.assertEqual(topic,"水土保持相关内容")
            self.assertTrue(any(text in e["quote"] for e in candidates))
            return {"claims":[{"text":"这份合成材料介绍了水土保持。","evidence":[candidates[0]]}],
                    "sections":[{"title":"内容","paragraphs":[[1]]}],"gaps":[]},{"model":"test"}
        with patch("trace_review.core.model_generate",side_effect=generate):
            job,_=self.task("/api/generate",imported["result"],
                            {"topic":"水土保持相关内容","requirements":"随意","mode":"model"},
                            config={"key":"test-only-key"})
        self.assertEqual(job["code"],200)
        self.assertGreater(job["result"]["run"]["writing"]["character_count"],0)
        self.assertTrue(job["result"]["run"]["claims"][0]["evidence"][0]["valid"])
        self.assertEqual(self.server.sessions,{})

    def test_empty_model_job_returns_failure_without_a_replacement_snapshot(self):
        with patch("trace_review.core.model_generate",return_value=({"claims":[],"gaps":[]},{"model":"test"})):
            job,_=self.task("/api/generate",self.sample,{"topic":"反馈","mode":"model"},config={"key":"test-only"})
        self.assertEqual(job["code"],400)
        self.assertEqual(job["progress"]["status"],"error")
        self.assertIn("未生成可用正文",job["result"]["error"])
        self.assertNotIn("run",job["result"])

    def test_job_result_is_cookie_scoped_and_ack_releases_it(self):
        job, identifier = self.task("/api/generate", self.sample, {"mode":"extract","topic":"隔离验证"}, ack=False)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.call("/api/browser/jobs/" + identifier, fresh=True)
        self.assertEqual(error.exception.code, 404)
        self.call("/api/browser/ack", {"job_id":identifier}, fresh=True)
        self.assertTrue(self.call("/api/browser/jobs/" + identifier)[1]["ready"])
        self.call("/api/browser/ack", {"job_id":identifier})
        with self.assertRaises(urllib.error.HTTPError):
            self.call("/api/browser/jobs/" + identifier)

    def test_key_discarded_immediately_when_processing_finishes(self):
        received = []
        def capture(config):
            received.append(dict(config))
            return {"ok":True,"model":"test"}
        with patch("web_app.probe_model", side_effect=capture) as probe:
            job, identifier = self.task("/api/connection", {}, config={"key":"visitor-only-secret"}, ack=False)
        self.assertEqual(job["code"], 200)
        self.assertEqual(received[0]["key"], "visitor-only-secret")
        self.assertEqual(probe.call_args.args[0]["key"], "")
        self.assertIsNone(self.server.browser_jobs.items[identifier]["session"])
        self.assertNotIn("visitor-only-secret", json.dumps(job))
        self.call("/api/browser/ack", {"job_id":identifier})

    def test_export_is_downloaded_without_persistent_workspace(self):
        job, identifier = self.task("/api/export", self.sample, ack=False)
        self.assertTrue(job["binary"])
        import urllib.request
        request = urllib.request.Request(self.base + "/api/browser/result/" + identifier,
                                         headers={"Host":"review.njfu.org","Cookie":self.cookie})
        with urllib.request.urlopen(request) as response:
            archive = response.read()
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            self.assertIn("ledger.json", zipped.namelist())
        self.call("/api/browser/ack", {"job_id":identifier})

    def test_legacy_migration_is_read_only_and_cookie_scoped(self):
        folder = Path(self.folder.name) / self.cookie.split("=",1)[1]
        folder.mkdir()
        data = {k:self.sample[k] for k in ("run","corpus","demo")}
        target = folder / "main.json"
        original = json.dumps(data, ensure_ascii=False)
        target.write_text(original, "utf-8")
        try:
            self.assertEqual(self.call("/api/browser/legacy")[1]["threads"], ["main"])
            self.assertEqual(self.call("/api/browser/legacy/main")[1]["run"], data["run"])
            self.assertEqual(self.call("/api/browser/legacy", fresh=True)[1]["threads"], [])
            self.assertEqual(target.read_text("utf-8"), original)
        finally:
            target.unlink()
            folder.rmdir()

    def test_repeated_job_id_does_not_repeat_model_work(self):
        started, release = threading.Event(), threading.Event()
        def slow(config):
            started.set();release.wait(3)
            return {"ok":True,"model":"test"}
        payload = {"job_id":"f"*32, "operation":"/api/connection", "config":{"key":"fake"}}
        with patch("web_app.probe_model", side_effect=slow) as probe:
            try:
                first = self.call("/api/browser/execute", payload)[1]
                self.assertTrue(started.wait(1))
                second = self.call("/api/browser/execute", payload)[1]
                self.assertEqual(first, second)
                self.assertEqual(probe.call_count, 1)
            finally:
                release.set()
            for _ in range(100):
                if self.call("/api/browser/jobs/"+first["job_id"])[1]["ready"]:
                    break
                time.sleep(.01)
        self.call("/api/browser/ack", {"job_id":first["job_id"]})


class BrowserJobLifetimeTests(unittest.TestCase):
    def test_finished_results_expire_and_running_jobs_survive(self):
        jobs = BrowserJobs(retention=10, capacity=2)
        first = jobs.add("owner", Session(public=True, transient=True))
        second = jobs.add("owner", Session(public=True, transient=True))
        with self.assertRaises(ValueError):
            jobs.add("owner", Session(public=True, transient=True))
        with patch("trace_review.browser_jobs.time.monotonic", return_value=100):
            jobs.finish(first, {"run":None}, 200, "application/json")
        with patch("trace_review.browser_jobs.time.monotonic", return_value=111):
            jobs.prune()
        self.assertNotIn(first, jobs.items)
        self.assertIn(second, jobs.items)
        jobs.acknowledge("owner", second)
        self.assertIn(second, jobs.items)


if __name__ == "__main__":
    unittest.main()
