import base64
import io
import json
import threading
import unittest
import tempfile
from unittest.mock import patch
import urllib.error
import urllib.request
import zipfile
from http.cookiejar import CookieJar

from web_app import Server


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server(("127.0.0.1", 0))
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def call(self, path, payload=None, raw=False, thread=None):
        request = urllib.request.Request(self.base + path)
        if thread:
            request.add_header("X-Trace-Conversation", thread)
        if payload is not None:
            request.data = json.dumps(payload).encode("utf-8")
            request.add_header("Content-Type", "application/json")
            request.add_header("X-Trace-Request", "1")
            request.add_header("Origin", self.base)
        with self.client.open(request) as response:
            return response.read() if raw else json.load(response)

    def test_static_assets_and_demo_session(self):
        with self.client.open(self.base) as response:
            self.assertIn("text/html", response.headers["Content-Type"])
            self.assertIn(b"viewContent", response.read())
        for asset in ("/app.js", "/style.css", "/favicon.svg", "/atmosphere.js", "/experience.css"):
            self.assertTrue(self.call(asset, raw=True))
        for font in ("trace-title-serif.woff2", "trace-title-sans.woff2"):
            with self.client.open(self.base + "/fonts/" + font) as response:
                self.assertEqual(response.headers["Content-Type"], "font/woff2")
                self.assertEqual(response.read(4), b"wOF2")
        state = self.call("/api/state")
        self.assertTrue(state["demo"])
        self.assertEqual(state["metrics"]["total"], 6)

    def test_import_generate_annotate_export_and_restore(self):
        self.call("/api/state")
        text = "反馈研究没有证明反馈提高了考试成绩。"
        encoded = base64.b64encode(text.encode("utf-8")).decode()
        imported = self.call("/api/import", {"files": [{"name": "研究.txt", "data": encoded}]})
        self.assertFalse(imported["demo"])
        self.assertEqual(len(imported["corpus"]["documents"]), 1)
        self.assertIsNone(imported["run"])
        run = self.call("/api/generate", {"topic": "反馈", "mode": "extract"})
        claim = run["run"]["claims"][0]
        annotated = self.call("/api/annotate", {"id": claim["id"], "label": "supported", "reviewer": "R01", "note": "核对否定词"})
        self.assertEqual(annotated["metrics"]["supported"], 1)
        self.assertEqual(self.call("/api/state")["run"]["claims"][0]["reviewer"], "R01")
        archive = self.call("/api/export", {}, raw=True)
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            self.assertEqual(set(zipped.namelist()), {"review.md", "audit.md", "ledger.json", "ledger.csv"})
            ledger = json.loads(zipped.read("ledger.json"))
        self.call("/api/demo", {})
        restored = self.call("/api/restore", {"run": ledger})
        self.assertEqual(restored["metrics"]["supported"], 1)

    def test_generation_receives_and_exports_writing_requirements(self):
        self.call("/api/state")
        with patch("web_app.make_run") as generate:
            # Reuse a real extract run to exercise HTTP and archive serialization.
            from trace_review.core import make_run
            corpus = self.call("/api/state")["corpus"]
            run = make_run(corpus, "反馈", requirements="不超过八百字")
            generate.return_value = run
            result = self.call("/api/generate", {"topic": "反馈", "mode": "model", "requirements": "不超过八百字"})
        self.assertEqual(generate.call_args.kwargs["requirements"], "不超过八百字")
        self.assertEqual(result["run"]["requirements"], "不超过八百字")
        with zipfile.ZipFile(io.BytesIO(self.call("/api/export", {}, raw=True))) as zipped:
            exported = json.loads(zipped.read("ledger.json"))
        self.assertEqual(exported["requirements"], "不超过八百字")

    def test_revision_export_restore_undo_and_stale_version(self):
        state=self.call("/api/state")
        original=state["run"]
        original["mode"]="model"
        self.call("/api/restore",{"run":original})
        anchor=original["claims"][-1]
        output={"message":"只新增一句。","operations":[{"op":"insert_after","anchor":anchor["id"],
                "claim":{"text":anchor["text"],"evidence":anchor["evidence"]}}]}
        response={"choices":[{"message":{"content":json.dumps(output)}}]}
        with patch("trace_review.revision.request_model",return_value=response):
            revised=self.call("/api/revise",{"instruction":"加一句话","base_revision":0})
        self.assertEqual(revised["run"]["claims"][:-1],original["claims"])
        self.assertEqual(len(revised["run"]["claims"]),len(original["claims"])+1)
        with self.assertRaises(urllib.error.HTTPError):
            self.call("/api/undo",{"base_revision":0})
        archive=self.call("/api/export",{},raw=True)
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            ledger=json.loads(zipped.read("ledger.json"))
        self.call("/api/restore",{"run":ledger})
        restored=self.call("/api/undo",{"base_revision":1})
        self.assertEqual(restored["run"]["article"],original["article"])
        self.assertEqual(restored["run"]["claims"],original["claims"])
        self.assertIn("撤销",restored["run"]["conversation"][-1]["content"])

    def test_rejected_revision_does_not_replace_current_draft(self):
        original=self.call("/api/state")["run"]
        output={"message":"错误的全文替换","operations":[{"op":"delete","id":original["claims"][0]["id"]}]}
        response={"choices":[{"message":{"content":json.dumps(output)}}]}
        with patch("trace_review.revision.request_model",return_value=response):
            with self.assertRaises(urllib.error.HTTPError):
                self.call("/api/revise",{"instruction":"只加一句话","base_revision":0})
        self.assertEqual(self.call("/api/state")["run"],original)

    def test_expansion_after_single_sentence_edit_persists_and_can_undo(self):
        original = self.call("/api/state")["run"]
        anchor = original["claims"][-1]
        operation = {"op":"insert_after", "anchor":anchor["id"],
                     "claim":{"text":anchor["text"], "evidence":anchor["evidence"]}}
        def response(count):
            return {"choices":[{"message":{"content":json.dumps({
                "message":f"新增{count}句。", "operations":[operation] * count
            })}}]}
        with patch("trace_review.revision.request_model", return_value=response(1)):
            first = self.call("/api/revise", {"instruction":"只加一句话", "base_revision":0})["run"]
        with patch("trace_review.revision.request_model", return_value=response(3)) as model:
            expanded = self.call("/api/revise", {
                "instruction":"不要只加一句。。。。加很多", "base_revision":1
            })["run"]
        self.assertEqual(model.call_count, 1)
        self.assertEqual(len(expanded["last_edit"]["added_ids"]), 3)
        prior = {c["id"]:c for c in first["claims"]}
        self.assertTrue(all(c == prior[c["id"]] for c in expanded["claims"] if c["id"] in prior))
        self.assertEqual(self.call("/api/state")["run"]["claims"], expanded["claims"])
        restored = self.call("/api/undo", {"base_revision":2})["run"]
        self.assertEqual(restored["claims"], first["claims"])
        self.assertEqual(restored["article"], first["article"])

    def test_progress_is_live_without_waiting_for_generation_lock(self):
        self.call("/api/state")
        started,release=threading.Event(),threading.Event()
        errors=[]
        from trace_review.core import make_run as real_make_run
        def generating(corpus,topic,mode,**kwargs):
            if "progress" not in kwargs: return real_make_run(corpus,topic,mode)
            kwargs["progress"]("retrieval","正在检索文献")
            started.set()
            release.wait(3)
            return real_make_run(corpus,topic,"extract")
        def submit():
            try:
                self.call("/api/generate",{"topic":"反馈","mode":"extract","operation_id":"live-test"})
            except Exception as exc:
                errors.append(exc)
        with patch("web_app.make_run",side_effect=generating):
            worker=threading.Thread(target=submit)
            worker.start()
            try:
                self.assertTrue(started.wait(2))
                with self.client.open(self.base+"/api/progress",timeout=1) as response:
                    progress=json.load(response)
                self.assertEqual(progress["operation_id"],"live-test")
                self.assertEqual(progress["status"],"running")
                self.assertEqual(progress["events"][-1]["message"],"正在检索文献")
                with urllib.request.urlopen(self.base+"/api/progress",timeout=1) as response:
                    self.assertEqual(json.load(response)["status"],"idle")
            finally:
                release.set()
                worker.join(timeout=3)
        self.assertFalse(errors)
        self.assertEqual(self.call("/api/progress")["status"],"complete")
        self.assertTrue(self.call("/api/state")["run"]["conversation"][-1]["process"])

    def test_failed_generation_progress_keeps_previous_draft(self):
        original=self.call("/api/state")["run"]
        with patch("web_app.make_run",side_effect=ValueError("本次生成未完成")):
            with self.assertRaises(urllib.error.HTTPError):
                self.call("/api/generate",{"topic":"反馈","mode":"model","operation_id":"failure"})
        self.assertEqual(self.call("/api/progress")["status"],"error")
        self.assertEqual(self.call("/api/state")["run"],original)

    def test_empty_extraction_does_not_replace_saved_draft_or_report_success(self):
        original=self.call("/api/state")["run"]
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.call("/api/generate",{"topic":"量子计算超导","mode":"extract"})
        self.assertIn("没有生成正文",json.load(error.exception)["error"])
        self.assertEqual(self.call("/api/state")["run"],original)
        self.assertEqual(self.call("/api/progress")["status"],"error")

    def test_model_key_never_returned_in_state_or_export(self):
        self.call("/api/state")
        state = self.call("/api/config", {"base": "https://example.invalid/v1", "model": "mock", "key": "test-secret-never-echo"})
        self.assertTrue(state["config"]["key_set"])
        self.assertNotIn("test-secret-never-echo", json.dumps(state))
        data = self.call("/api/export", {}, raw=True)
        with zipfile.ZipFile(io.BytesIO(data)) as zipped:
            self.assertNotIn(b"test-secret-never-echo", zipped.read("ledger.json"))
        cleared = self.call("/api/config", {"base": "", "model": "", "clear_key": True})
        self.assertFalse(cleared["config"]["key_set"])

    def test_sessions_are_isolated(self):
        self.call("/api/state")
        self.call("/api/config", {"base": "", "model": "private-session-model", "key": ""})
        other = urllib.request.build_opener()
        with other.open(self.base + "/api/state") as response:
            state = json.load(response)
        self.assertNotEqual(state["config"]["model"], "private-session-model")

    def test_cross_origin_and_missing_request_header_rejected(self):
        for headers in ({"Origin": "https://untrusted.invalid", "X-Trace-Request": "1"}, {}):
            request = urllib.request.Request(self.base + "/api/demo", data=b"{}", headers=headers)
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.client.open(request)
            self.assertEqual(error.exception.code, 403)

    def test_missing_model_configuration_returns_visible_error(self):
        self.call("/api/state")
        self.call("/api/config", {"base": "", "model": "", "clear_key": True})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.call("/api/generate", {"topic": "反馈", "mode": "model"})
        self.assertEqual(error.exception.code, 400)
        self.assertIn("TRACE", json.load(error.exception)["error"])

    def test_connection_probe_reports_configured_model_without_key(self):
        result = {"ok": True, "model": "deepseek-flash", "models": ["deepseek-flash"]}
        with patch("web_app.probe_model", return_value=result):
            state = self.call("/api/connection", {})
        self.assertEqual(state["config"]["connection"], result)
        self.assertNotIn("key", state["config"])

    def test_persisted_settings_survive_new_browser_session(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {"TRACE_CONFIG_DIR": folder}):
            self.call("/api/state")
            self.call("/api/config", {"base": "https://api.deepseek.com", "model": "deepseek-flash", "key": "persistent-test-key", "persist": True})
            with urllib.request.urlopen(self.base + "/api/state") as response:
                state = json.load(response)
        self.assertTrue(state["config"]["key_set"])
        self.assertNotIn("persistent-test-key", json.dumps(state))

    def test_conversations_keep_separate_manuscripts_and_sources(self):
        original = self.call("/api/state")
        second = self.call("/api/threads/new", {})
        self.assertIsNone(second["run"])
        self.assertEqual(second["corpus"], original["corpus"])
        second_id = second["thread_id"]
        result = self.call("/api/generate", {"topic": "第二篇反馈研究", "mode": "extract"}, thread=second_id)
        self.assertEqual(result["run"]["topic"], "第二篇反馈研究")
        self.assertEqual(self.call("/api/state")["run"], original["run"])
        text = base64.b64encode("第二份独立文献。".encode()).decode()
        self.call("/api/import", {"files": [{"name": "第二篇.txt", "data": text}]}, thread=second_id)
        self.assertEqual(self.call("/api/state")["corpus"], original["corpus"])
        self.assertEqual(len(self.call("/api/threads")["threads"]), 2)
        # Another browser cannot address this browser's conversation.
        request = urllib.request.Request(self.base + "/api/state", headers={"X-Trace-Conversation":second_id})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(error.exception.code, 404)

    def test_two_generations_run_concurrently_and_state_remains_readable(self):
        original = self.call("/api/state")
        second = self.call("/api/threads/new", {})["thread_id"]
        started = [threading.Event(), threading.Event()]
        release = threading.Event()
        errors = []
        from trace_review.core import make_run as real_make_run
        def generating(corpus, topic, mode, **kwargs):
            if "progress" not in kwargs:
                return real_make_run(corpus, topic, mode)
            index = 0 if topic == "第一篇" else 1
            kwargs["progress"]("generation", topic)
            started[index].set()
            release.wait(5)
            run = real_make_run(corpus, "反馈", "extract")
            run["topic"] = topic
            return run
        def submit(thread, topic):
            try:
                self.call("/api/generate", {"topic":topic,"mode":"extract"}, thread=thread)
            except Exception as exc:
                errors.append(exc)
        with patch("web_app.make_run", side_effect=generating):
            workers = [threading.Thread(target=submit,args=("main","第一篇")),
                       threading.Thread(target=submit,args=(second,"第二篇"))]
            for worker in workers:
                worker.start()
            try:
                self.assertTrue(all(event.wait(2) for event in started))
                pending = self.call("/api/state")
                self.assertEqual(pending["run"], original["run"])
                self.assertEqual(pending["progress"]["status"], "running")
                self.assertTrue(all(t["status"] == "running" for t in self.call("/api/threads")["threads"]))
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.call("/api/demo", {})
                self.assertEqual(error.exception.code, 409)
                # Starting another conversation must not wait for either model.
                fresh = self.call("/api/threads/new", {})
                self.assertIsNone(fresh["run"])
            finally:
                release.set()
                for worker in workers:
                    worker.join(3)
        self.assertFalse(errors)
        self.assertEqual(self.call("/api/state")["run"]["topic"], "第一篇")
        self.assertEqual(self.call("/api/state",thread=second)["run"]["topic"], "第二篇")

    def test_workspace_persists_manuscripts_without_credentials(self):
        from pathlib import Path
        from web_app import Workspace, Session
        with tempfile.TemporaryDirectory() as folder:
            workspace = Workspace(Path(folder))
            session = workspace.threads["main"]
            session.config["key"] = "workspace-secret-not-for-disk"
            session.run["topic"] = "可恢复的研究"
            workspace.save("main", session)
            for identifier in ("f" * 24, "a" * 24):
                sibling = Session()
                workspace.threads[identifier] = sibling
                workspace.save(identifier, sibling)
            stored = (Path(folder) / "main.json").read_text(encoding="utf-8")
            self.assertNotIn("workspace-secret-not-for-disk", stored)
            restored = Workspace(Path(folder))
            self.assertEqual(restored.threads["main"].run, session.run)
            self.assertEqual([s["id"] for s in restored.listing()], ["main", "f" * 24, "a" * 24])


if __name__ == "__main__":
    unittest.main()
