"""Local browser application. Run: python web_app.py [--no-browser] [--port 8765]."""
from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime, timezone
import io
import json
import os
import re
import secrets
import tempfile
import threading
import time
import webbrowser
import zipfile
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from trace_review.revision import revise_run, undo_revision
from trace_review.core import LABELS, annotate, export_run, make_run, metrics, read_documents, recheck, probe_model
from trace_review.config import load_model_config, save_model_config
from trace_review.public_access import PublicLimits, visitor_config, visitor_settings, public_origin, check_corpus
from trace_review.browser_jobs import BrowserJobs

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "web"
MAX_BODY = 50 * 1024 * 1024


class Session:
    def __init__(self, public=False, transient=False):
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.lock = threading.RLock()
        self.progress_lock = threading.Lock()
        self.progress = {"operation_id":None,"status":"idle","events":[]}
        self.public = public
        self.transient = transient
        self.background_job = False
        self.config = visitor_config() if public else load_model_config()
        self.connection = None
        if transient:
            self.corpus, self.run, self.is_demo = {"documents":[], "warnings":[]}, None, False
        else:
            self.demo()
        self.pending_request = None
        self.saved_state = self.state()

    def demo(self):
        self.corpus = read_documents(sorted((ROOT / "data/demo").glob("*.txt")))
        self.run = make_run(self.corpus, "反馈方式与学习效果", "extract")
        self.is_demo = True

    def commit(self):
        self.saved_state = self.state() if self.transient else copy.deepcopy(self.state())

    def readable_state(self):
        if self.lock.acquire(blocking=False):
            try:
                return copy.deepcopy(self.state())
            finally:
                self.lock.release()
        return copy.deepcopy(self.saved_state)

    def begin_progress(self, operation_id):
        with self.progress_lock:
            self.progress = {"operation_id":str(operation_id or secrets.token_hex(12))[:100],"status":"running","events":[]}

    def report_progress(self, stage, message):
        with self.progress_lock:
            self.progress["events"].append({"stage":stage,"message":message,"at":datetime.now(timezone.utc).isoformat()})
            if stage in ("complete","error"):
                self.progress["status"] = stage

    def progress_state(self):
        with self.progress_lock:
            result = copy.deepcopy(self.progress)
            # Completion is visible only after the manuscript is committed.
            if self.background_job:
                result["status"] = "running"
            return result

    def finish_progress(self, summary):
        self.report_progress("complete",summary)
        process=self.progress_state()["events"]
        if self.run is not None and self.run.get("conversation"):
            self.run["conversation"][-1]["process"] = process

    def state(self):
        # Credentials never leave the Python process in API responses.
        return {"corpus": self.corpus, "run": self.run, "demo": self.is_demo,
                "labels": LABELS, "metrics": metrics(self.run) if self.run else None,
                "config": {"base": self.config["base"], "model": self.config["model"],
                           "key_set": bool(self.config["key"]), "connection": self.connection,
                           "visitor_key": self.public}}


class Workspace:
    """One browser owns multiple independently locked, locally saved conversations."""
    def __init__(self, folder=None, public=False, initial=None):
        self.folder = folder
        self.public = public
        self.last_access = time.monotonic()
        self.lock = threading.RLock()
        self.threads = {}
        if folder and folder.exists():
            for file in folder.glob("*.json"):
                if not re.fullmatch(r"main|[a-f0-9]{24}", file.stem):
                    continue
                try:
                    data = json.loads(file.read_text(encoding="utf-8"))
                    session = Session(public=public)
                    session.corpus, session.run, session.is_demo = data["corpus"], data["run"], data["demo"]
                    session.created_at = data.get("created_at") or (session.run or {}).get("created_at") or session.created_at
                    session.commit()
                    self.threads[file.stem] = session
                except (ValueError, KeyError, OSError):
                    continue
        if initial is not None:
            self.threads = {"main":initial}
        elif "main" not in self.threads:
            self.threads["main"] = Session(public=public)

    def save(self, identifier, session):
        session.commit()
        # Older open pages still use disk-backed workspaces until they refresh and
        # migrate. Browser-owned jobs have no folder and never persist here.
        if self.folder:
            self.folder.mkdir(parents=True, exist_ok=True)
            data = {key: session.saved_state[key] for key in ("corpus", "run", "demo")}
            data["created_at"] = session.created_at
            target = self.folder / (identifier + ".json")
            temp = target.with_suffix(".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            temp.replace(target)

    def listing(self):
        with self.lock:
            items = sorted(self.threads.items(), key=lambda item: (item[0] != "main", item[1].created_at, item[0]))
        return [{"id": key, "title": (s.pending_request or {}).get("topic") if s.progress_state()["status"] == "running" and (s.pending_request or {}).get("topic") else (s.saved_state.get("run") or {}).get("topic") or "新的研究",
                 "status": s.progress_state()["status"],
                 "has_document": bool(s.saved_state.get("run")),
                 "source_count": len(s.saved_state["corpus"]["documents"])} for key, s in items]


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, storage=None, origin=None):
        self.public_origin = public_origin(origin) if origin else None
        self.public = bool(self.public_origin)
        self.limits = PublicLimits() if self.public else None
        super().__init__(address, Handler)
        self.storage = Path(storage) if storage else None
        self.sessions = {}
        self.sessions_lock = threading.Lock()
        self.browser_jobs = BrowserJobs()

    def service_actions(self):
        if self.public:
            self.browser_jobs.prune()
            with self.sessions_lock:
                for key, space in list(self.sessions.items()):
                    if time.monotonic() - space.last_access > 180 and not any(
                            s.background_job or s.progress_state()["status"] == "running" for s in space.threads.values()):
                        del self.sessions[key]


class Handler(BaseHTTPRequestHandler):
    server_version = "TraceReview/2"

    def log_message(self, fmt, *args):
        # Never log request bodies, uploaded text or model credentials.
        pass

    def browser_identity(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            cookie = SimpleCookie()
        identifier = cookie["trace_session"].value if "trace_session" in cookie else ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,64}", identifier):
            identifier = secrets.token_urlsafe(32)
        self.session_cookie = identifier
        return identifier

    def session(self):
        identifier = self.browser_identity()
        with self.server.sessions_lock:
            if identifier not in self.server.sessions:
                if self.server.public and len(self.server.sessions) >= 200:
                    raise ValueError("研究空间暂时已满，请稍后再试。")
                folder = self.server.storage / identifier if self.server.storage else None
                self.server.sessions[identifier] = Workspace(folder, public=self.server.public)
            self.workspace = self.server.sessions[identifier]
            self.workspace.last_access = time.monotonic()
        self.session_cookie = identifier
        self.thread_id = self.headers.get("X-Trace-Conversation", "main")
        with self.workspace.lock:
            if self.thread_id not in self.workspace.threads:
                raise ValueError("这个会话不存在，请重新选择。")
            return self.workspace.threads[self.thread_id]

    def session_state(self, session):
        data = session.readable_state()
        data["thread_id"] = self.thread_id
        data["threads"] = self.workspace.listing()
        data["progress"] = session.progress_state()
        data["pending_request"] = session.pending_request if data["progress"]["status"] in ("running", "error") else None
        return data

    def local_request(self):
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origins = {f"http://{host}" for host in allowed}
        if self.server.public:
            allowed.add(urlparse(self.server.public_origin).netloc)
            origins.add(self.server.public_origin)
        if self.headers.get("Host") not in allowed:
            return False
        origin = self.headers.get("Origin")
        return not origin or origin in origins

    def insecure_public_request(self):
        # Cloudflare supplies the visitor's protocol; the local tunnel itself uses HTTP.
        return (self.server.public
                and self.headers.get("Host") == urlparse(self.server.public_origin).netloc
                and self.headers.get("X-Forwarded-Proto", "").lower() == "http")

    def redirect_to_https(self):
        self.send_response(308)
        self.send_header("Location", self.server.public_origin + "/" + self.path.lstrip("/"))
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def send(self, data, content_type="application/json; charset=utf-8", status=200, download=None, cache=None):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        elif isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache or "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if self.server.public and self.headers.get("Host") == urlparse(self.server.public_origin).netloc:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        if hasattr(self, "session_cookie"):
            secure = "; Secure" if self.server.public else ""
            self.send_header("Set-Cookie", f"trace_session={self.session_cookie}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000{secure}")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.end_headers()
        try:
            if self.command != "HEAD":
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # A page reload does not cancel a completed or background model job.
            pass

    def do_GET(self):
        if self.insecure_public_request():
            return self.redirect_to_https()
        if not self.local_request():
            return self.send({"error": "只允许从本机工作台访问。"}, status=403)
        path = urlparse(self.path).path
        if path == "/api/bootstrap":
            self.browser_identity()
            return self.send({"browser_storage":self.server.public,
                              "sample":Session(public=True).state() if self.server.public else None})
        if self.server.public and path.startswith("/api/browser/"):
            return self.browser_get(path)
        if path in ("/api/progress", "/api/state", "/api/threads"):
            try:
                session = self.session()
                if path == "/api/progress":
                    return self.send(session.progress_state())
                if path == "/api/threads":
                    return self.send({"threads": self.workspace.listing()})
                return self.send(self.session_state(session))
            except ValueError as exc:
                return self.send({"error": str(exc)}, status=404)
        files = {"/": ("index.html", "text/html; charset=utf-8"),
                 "/style.css": ("style.css", "text/css; charset=utf-8"),
                 "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                 "/browser-storage.js": ("browser-storage.js", "text/javascript; charset=utf-8"),
                 "/atmosphere.js": ("atmosphere.js", "text/javascript; charset=utf-8"),
                 "/experience.css": ("experience.css", "text/css; charset=utf-8"),
                 "/fonts/trace-title-serif.woff2": ("fonts/trace-title-serif.woff2", "font/woff2"),
                 "/fonts/trace-title-sans.woff2": ("fonts/trace-title-sans.woff2", "font/woff2"),
                  "/fonts/trace-ink-kai.woff2": ("fonts/trace-ink-kai.woff2", "font/woff2"),
                  "/fonts/trace-ink-running.woff2": ("fonts/trace-ink-running.woff2", "font/woff2"),
                  "/fonts/trace-ink-grass.woff2": ("fonts/trace-ink-grass.woff2", "font/woff2"),
                  "/fonts/trace-ink-seal.woff2": ("fonts/trace-ink-seal.woff2", "font/woff2"),
                 "/favicon.svg": ("favicon.svg", "image/svg+xml")}
        if path in files:
            name, mime = files[path]
            return self.send((STATIC / name).read_bytes(), mime,
                             cache="public, max-age=604800" if mime == "font/woff2" else None)
        self.send({"error": "页面不存在。"}, status=404)

    def do_HEAD(self):
        return self.do_GET()

    def do_POST(self):
        if self.insecure_public_request():
            return self.send({"error": "请通过 HTTPS 地址打开研究空间后重试。"}, status=426)
        if not self.local_request() or self.headers.get("X-Trace-Request") != "1":
            return self.send({"error": "请求来源无效。"}, status=403)
        try:
            if self.server.public:
                client = self.headers.get("CF-Connecting-IP", self.client_address[0])[:64]
                if not self.server.limits.allow(client, "write", 60, 60):
                    return self.send({"error": "操作较频繁，请一分钟后再试。"}, status=429)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                return self.send({"error": "单次导入请控制在 35 MB 以内。"}, status=413)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("请求数据无效。")
            path = urlparse(self.path).path
            if self.server.public and path.startswith("/api/browser/"):
                return self.browser_post(path, body, client)
            session = self.session()
            if path == "/api/threads/new":
                if self.server.public and len(self.workspace.threads) >= 30:
                    raise ValueError("当前空间最多创建 30 个研究。")
                fresh = Session(public=self.server.public)
                fresh.corpus = copy.deepcopy(session.readable_state()["corpus"])
                fresh.is_demo = session.is_demo
                fresh.config = copy.deepcopy(session.config)
                fresh.run = None
                fresh.commit()
                self.thread_id = secrets.token_hex(12)
                with self.workspace.lock:
                    self.workspace.threads[self.thread_id] = fresh
                self.workspace.save(self.thread_id, fresh)
                return self.send(self.session_state(fresh))
            if session.background_job or not session.lock.acquire(blocking=False):
                return self.send({"error": "此会话正在处理，可以先切换或新建其他会话。"}, status=409)
            if self.server.public and path in ("/api/generate", "/api/revise"):
                if not self.server.limits.allow(client, "generation", 30, 3600):
                    session.lock.release()
                    return self.send({"error": "每小时最多提交 30 次写作请求，请稍后继续。"}, status=429)
                if not self.server.limits.jobs.acquire(blocking=False):
                    session.lock.release()
                    return self.send({"error": "已有两篇综述正在处理，请稍后再提交。"}, status=429)
                body["operation_id"] = str(body.get("operation_id") or secrets.token_hex(12))[:100]
                session.pending_request = {"kind": path, "topic": str(body.get("topic", "")),
                                           "content": str(body.get("instruction") or ("主题：" + str(body.get("topic", "")) + "\n要求：" + str(body.get("requirements", ""))))}
                session.begin_progress(body["operation_id"])
                session.background_job = True
                session.lock.release()
                response = self.session_state(session)
                def work():
                    session.lock.acquire()
                    self.perform_mutation(session, path, body, background=True)
                threading.Thread(target=work, daemon=True).start()
                return self.send(response, status=202)
            return self.perform_mutation(session, path, body)
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
            self.send({"error": str(exc) or "请求失败，请检查输入。"}, status=400)

    def browser_get(self, path):
        owner = self.browser_identity()
        if path == "/api/browser/legacy":
            folder = self.server.storage / owner if self.server.storage else None
            identifiers = sorted(p.stem for p in folder.glob("*.json")
                                 if re.fullmatch(r"main|[a-f0-9]{24}", p.stem)) if folder and folder.exists() else []
            return self.send({"threads":identifiers})
        if path.startswith("/api/browser/legacy/"):
            identifier = path.rsplit("/", 1)[-1]
            if self.server.storage and re.fullmatch(r"main|[a-f0-9]{24}", identifier):
                target = self.server.storage / owner / (identifier + ".json")
                if target.is_file():
                    try:
                        saved = json.loads(target.read_text("utf-8"))
                        session = Session(public=True, transient=True)
                        session.corpus, session.run, session.is_demo = saved["corpus"], saved["run"], saved["demo"]
                        return self.send({**session.state(), "thread_id":identifier, "created_at":saved.get("created_at")})
                    except (ValueError, KeyError, OSError):
                        return self.send({"error":"旧稿件暂时无法读取，请保留浏览器数据并联系站主。"}, status=400)
        identifier = path.rsplit("/", 1)[-1]
        if re.fullmatch(r"[a-f0-9]{32}", identifier):
            job = self.server.browser_jobs.get(owner, identifier)
            if job:
                if path.startswith("/api/browser/result/") and job["ready"]:
                    return self.send(job["result"], job["mime"], status=job["code"])
                if path.startswith("/api/browser/jobs/"):
                    if job.get("mime") == "application/zip":
                        job = {**job, "result":None, "binary":True}
                    return self.send(job)
        return self.send({"error":"临时任务已释放或服务已重启；浏览器中的原稿仍保留。"}, status=404)

    def browser_post(self, path, body, client):
        owner = self.browser_identity()
        if path == "/api/browser/ack":
            self.server.browser_jobs.acknowledge(owner, str(body.get("job_id", "")))
            return self.send({"ok":True})
        if path != "/api/browser/execute":
            return self.send({"error":"接口不存在。"}, status=404)
        operation = body.get("operation")
        allowed = {"/api/import", "/api/generate", "/api/revise", "/api/undo", "/api/annotate",
                   "/api/restore", "/api/export", "/api/connection"}
        if operation not in allowed:
            raise ValueError("未知处理操作。")
        requested_id = str(body.get("job_id") or secrets.token_hex(16))
        if not re.fullmatch(r"[a-f0-9]{32}", requested_id):
            raise ValueError("任务编号无效。")
        if self.server.browser_jobs.get(owner, requested_id):
            return self.send({"job_id":requested_id}, status=202)
        if operation in ("/api/generate", "/api/revise") and not self.server.limits.allow(client, "generation", 30, 3600):
            return self.send({"error":"每小时最多提交 30 次写作请求，请稍后继续。"}, status=429)
        snapshot = body.get("snapshot") or {}
        session = Session(public=True, transient=True)
        session.run = snapshot.get("run")
        if session.run is not None and (not isinstance(session.run, dict) or session.run.get("schema_version") != 1):
            raise ValueError("稿件格式不正确。")
        session.corpus = snapshot.get("corpus") or (session.run or {}).get("corpus") or {"documents":[], "warnings":[]}
        check_corpus(session.corpus)
        if session.run:
            check_corpus(session.run["corpus"])
        session.is_demo = bool(snapshot.get("demo"))
        session.config = visitor_settings(body.get("config") or {}, visitor_config())
        parameters = body.get("body") or {}
        if not isinstance(parameters, dict):
            raise ValueError("请求数据无效。")
        if not self.server.limits.jobs.acquire(blocking=False):
            return self.send({"error":"已有两个任务正在处理，请稍后再提交。"}, status=429)
        try:
            identifier = self.server.browser_jobs.add(owner, session, requested_id)
        except Exception:
            self.server.limits.jobs.release()
            raise
        session.begin_progress(parameters.get("operation_id"))
        session.background_job = True
        self.workspace = Workspace(public=True, initial=session)
        self.thread_id = "main"
        def work():
            result = {"error":"处理未完成，浏览器中的原稿已保留。"}
            code, mime = 500, "application/json; charset=utf-8"
            def capture(data, content_type="application/json; charset=utf-8", status=200, download=None):
                nonlocal result, code, mime
                if isinstance(data, dict) and "corpus" in data:
                    data = {key:data[key] for key in ("run", "demo", "metrics")}
                    if data["run"] is None:
                        data["corpus"] = session.corpus
                    if session.connection:
                        data["connection"] = session.connection
                result, code, mime = data, status, content_type
            try:
                session.lock.acquire()
                self.perform_mutation(session, operation, parameters, respond=capture)
            finally:
                self.server.browser_jobs.finish(identifier, result, code, mime)
                self.server.limits.jobs.release()
        threading.Thread(target=work, daemon=True).start()
        return self.send({"job_id":identifier}, status=202)

    def perform_mutation(self, session, path, body, background=False, respond=None):
        reply = respond or self.send
        try:
            try:
                if path == "/api/demo":
                    session.demo()
                elif path == "/api/import":
                    files = body.get("files", [])
                    if not files or len(files) > 100:
                        raise ValueError("请一次选择 1 至 100 篇文献。")
                    with tempfile.TemporaryDirectory() as folder:
                        paths = []
                        for index, item in enumerate(files):
                            name = str(item["name"]).replace("\\", "/").split("/")[-1]
                            if Path(name).suffix.lower() not in (".pdf", ".txt", ".md"):
                                raise ValueError("仅支持 PDF、TXT 和 Markdown 文献。")
                            subdir = Path(folder) / str(index)
                            subdir.mkdir()
                            target = subdir / name
                            target.write_bytes(base64.b64decode(item["data"], validate=True))
                            paths.append(target)
                        incoming = read_documents(paths)
                    documents = [] if session.is_demo else list(session.corpus["documents"])
                    known = {d["id"] for d in documents}
                    for doc in incoming["documents"]:
                        doc["path"] = "browser-upload:" + doc["filename"]
                        if doc["id"] not in known:
                            documents.append(doc)
                            known.add(doc["id"])
                    corpus = {"documents": documents, "warnings": incoming["warnings"]}
                    if self.server.public:
                        check_corpus(corpus)
                    session.corpus = corpus
                    session.is_demo, session.run = False, None
                elif path == "/api/generate":
                    session.pending_request = {"kind": path, "topic": str(body.get("topic", "")), "content": "主题："+str(body.get("topic", ""))+"\n要求："+str(body.get("requirements", ""))}
                    session.begin_progress(body.get("operation_id"))
                    mode = body.get("mode", "extract")
                    if mode not in ("extract", "model"):
                        raise ValueError("未知生成模式。")
                    if self.server.public and mode == "model" and not session.config["key"]:
                        raise ValueError("请先在模型连接中填写你自己的 DeepSeek API Key。")
                    generated = make_run(session.corpus, str(body.get("topic", "")), mode,
                                           limit=36 if mode == "model" else 12, model_config=session.config,
                                           requirements=body.get("requirements", ""), progress=session.report_progress)
                    if not generated.get("claims") or generated["writing"]["character_count"] <= 0:
                        explanation = ("原文摘录按关键词匹配，请换用文献中的关键词；英文文献配中文主题时可使用 AI 综述。"
                                       if mode == "extract" else "请检查原文是否成功读取，或调整主题后重试。")
                        raise ValueError("本次没有生成正文，原稿已保留。" + explanation)
                    session.run = generated
                    if mode == "model" and session.run["generation"].get("model"):
                        session.connection = {"ok": True, "model": session.run["generation"]["model"],
                                              "checked_at": session.run["created_at"], "message": "真实 AI 生成已成功完成。"}
                    words = session.run["writing"]["character_count"]
                    summary = f"已生成《{session.run['article']['title'] or session.run['topic']}》，正文 {words} 字。"
                    if not session.run["writing"]["met"]:
                        summary += " " + " ".join(session.run["writing"]["issues"])
                    session.run["conversation"] = [
                        {"role":"user","content":"主题："+session.run["topic"]+"\n要求："+session.run.get("requirements","")},
                        {"role":"assistant","content":summary,"document_ready":True}]
                    session.finish_progress(summary)
                elif path in ("/api/revise", "/api/undo"):
                    if not session.run:
                        raise ValueError("请先生成综述。")
                    if body.get("base_revision", 0) != session.run.get("revision_number", 0):
                        raise ValueError("稿件已更新，请刷新页面后继续修改。")
                    if path == "/api/revise":
                        if self.server.public and not session.config["key"]:
                            raise ValueError("请先在模型连接中填写你自己的 DeepSeek API Key。")
                        session.pending_request = {"kind": path, "content": str(body.get("instruction", ""))}
                        session.begin_progress(body.get("operation_id"))
                        session.run = revise_run(session.run, body.get("instruction", ""), session.config, body.get("selected_id"), progress=session.report_progress)
                        edit=session.run["last_edit"]
                        session.run["conversation"][-1]["changes"]={key:edit[key] for key in ("added_ids","updated_ids","removed_ids")}
                        session.finish_progress(edit["summary"])
                    else:
                        session.run = undo_revision(session.run)
                elif path == "/api/annotate":
                    if not session.run:
                        raise ValueError("请先生成草稿。")
                    claim = next((c for c in session.run["claims"] if c["id"] == body.get("id")), None)
                    if not claim:
                        raise ValueError("没有找到这条陈述。")
                    annotate(claim, str(body.get("label", "pending")),
                             str(body.get("reviewer", "")), str(body.get("note", "")))
                elif path == "/api/config":
                    config = visitor_settings(body, session.config) if self.server.public else {"base": str(body.get("base", "")).strip(),
                              "model": str(body.get("model", "")).strip(),
                              "key": str(body.get("key", "")).strip() or session.config["key"]}
                    if body.get("clear_key"):
                        config["key"] = ""
                    if body.get("persist") and not self.server.public:
                        save_model_config(config)
                    with self.workspace.lock:
                        for sibling in self.workspace.threads.values():
                            sibling.config = copy.deepcopy(config)
                            sibling.connection = None
                elif path == "/api/connection":
                    if self.server.public and not session.config["key"]:
                        raise ValueError("请先填写你自己的 DeepSeek API Key。")
                    session.connection = probe_model(session.config)
                elif path == "/api/restore":
                    run = body.get("run")
                    if not isinstance(run, dict) or run.get("schema_version") != 1:
                        raise ValueError("请选择本项目导出的 ledger.json。")
                    if self.server.public:
                        check_corpus(run["corpus"])
                    recheck(run)
                    metrics(run)
                    session.run = run
                    session.corpus = run["corpus"]
                    session.is_demo = any("合成演示材料" in p["text"] for d in session.corpus["documents"] for p in d["pages"])
                elif path == "/api/export":
                    if not session.run:
                        raise ValueError("请先生成草稿。")
                    run = copy.deepcopy(session.run)
                    with tempfile.TemporaryDirectory() as folder:
                        export_run(run, folder)
                        buffer = io.BytesIO()
                        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                            for file in Path(folder).iterdir():
                                archive.write(file, file.name)
                    return reply(buffer.getvalue(), "application/zip", download="trace-review-ledger.zip")
                else:
                    return reply({"error": "接口不存在。"}, status=404)
                self.workspace.save(self.thread_id, session)
                if not background:
                    return reply(self.session_state(session))
            finally:
                session.lock.release()
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
            if "session" in locals() and "path" in locals() and path in ("/api/generate","/api/revise"):
                session.report_progress("error",str(exc) or "处理未完成，原稿已保留。")
            if not background:
                reply({"error": str(exc) or "操作失败，请检查输入。"}, status=400)
        except Exception:
            if "session" in locals() and "path" in locals() and path in ("/api/generate","/api/revise"):
                session.report_progress("error","处理未完成，原稿已保留。")
            if not background:
                reply({"error": "操作失败，请检查文献格式或模型接口配置。"}, status=500)
        finally:
            if background:
                session.background_job = False
                self.server.limits.jobs.release()


def main():
    parser = argparse.ArgumentParser(description="循证综述 · 浏览器工作台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--public-origin", help="公开站的 HTTPS 地址，启用访客自带 Key 模式")
    parser.add_argument("--storage", help="独立会话存储目录")
    args = parser.parse_args()
    try:
        default_folder = "public-workspaces" if args.public_origin else "workspaces"
        storage = Path(args.storage or os.environ.get("TRACE_WORKSPACE_DIR", str(Path(os.environ.get("LOCALAPPDATA", ROOT / "outputs")) / "TraceReview" / default_folder)))
        server = Server(("127.0.0.1", args.port), storage, origin=args.public_origin)
    except OSError:
        if args.public_origin:
            raise SystemExit("公开站端口已被占用，请先停止旧的公开站进程。")
        server = Server(("127.0.0.1", 0), storage)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"TraceReview ready: {url}", flush=True)
    print("Keep this terminal open. Press Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
