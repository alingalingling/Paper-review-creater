"""Exercise the deployed HTTPS app using synthetic text, without paid AI calls."""
import base64
import json
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

ORIGIN = "https://review.njfu.org"
ROOT = Path(__file__).resolve().parents[1]


def verify():
    report = {"url": ORIGIN, "checks": []}
    with tempfile.TemporaryDirectory(prefix="review-verify-") as folder:
        directory = Path(folder)
        cookie = directory / "cookies.txt"
        def call(path, payload=None, thread="main", fresh=False, origin=ORIGIN, base=ORIGIN):
            headers = directory / "headers.txt"
            args = ["curl.exe", "--silent", "--show-error", "--connect-timeout", "10", "--max-time", "30",
                    "--dump-header", str(headers), "--write-out", "\n%{http_code}",
                    "--header", "X-Trace-Conversation: " + thread]
            if not fresh:
                args += ["--cookie", str(cookie), "--cookie-jar", str(cookie)]
            if payload is not None:
                body = directory / "body.json"
                body.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                args += ["--header", "Content-Type: application/json", "--header", "X-Trace-Request: 1",
                         "--header", "Origin: " + origin, "--data-binary", "@" + str(body)]
            output = subprocess.run(args + [base + path], check=True, capture_output=True).stdout
            data, status = output.rsplit(b"\n", 1)
            return int(status), data, headers.read_text("utf-8")
        status, _, headers = call("/?from=compatibility-check", fresh=True, base="http://review.njfu.org")
        assert status == 308 and "location: https://review.njfu.org/?from=compatibility-check" in headers.lower()
        report["checks"].append("HTTP entry redirects to HTTPS before browser storage is initialized")
        status, html, headers = call("/")
        assert status == 200 and b"studioShell" in html
        assert "strict-transport-security: max-age=31536000" in headers.lower()
        report["checks"].append("HTTPS homepage")
        for path in ("/app.js", "/browser-storage.js", "/experience.css", "/fonts/trace-title-sans.woff2"):
            assert call(path)[0] == 200
        report["checks"].append("JavaScript, stylesheet and font assets")
        font_files = ("trace-ink-kai.woff2", "trace-ink-running.woff2", "trace-ink-grass.woff2", "trace-ink-seal.woff2")
        for name in font_files:
            status, data, font_headers = call("/fonts/" + name + "?v=1")
            assert status == 200 and data[:4] == b"wOF2"
            assert data == (ROOT / "web/fonts" / name).read_bytes()
            assert "max-age=604800" in font_headers
        assert b'bootScreen' in html and b'studio-loading' in html
        report["checks"].append("Four self-hosted calligraphy fonts and composed loading screen")
        status, raw, headers = call("/api/bootstrap")
        bootstrap = json.loads(raw)
        state = bootstrap["sample"]
        assert status == 200 and bootstrap["browser_storage"]
        assert state["config"]["visitor_key"] and not state["config"]["key_set"]
        assert "Secure" in headers and "HttpOnly" in headers
        report["checks"].append("Visitor-owned key configuration and secure session cookie")
        assert json.loads(call("/api/browser/legacy")[1])["threads"] == []

        def task(operation, snapshot, body=None, acknowledge=True):
            payload = {"operation":operation, "job_id":uuid.uuid4().hex, "body":body or {},
                       "snapshot":{key:snapshot[key] for key in ("run","demo","corpus") if key in snapshot}}
            status, raw, _ = call("/api/browser/execute", payload)
            assert status == 202, raw.decode()
            identifier = json.loads(raw)["job_id"]
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                status, raw, _ = call("/api/browser/jobs/" + identifier)
                assert status == 200
                job = json.loads(raw)
                if job["ready"]:
                    if acknowledge:
                        assert call("/api/browser/ack", {"job_id":identifier})[0] == 200
                    return job, identifier
                time.sleep(.5)
            raise AssertionError("Temporary task did not complete")
        text = "【部署验证专用合成材料】该测试仅用于检查网站的文件导入、原文摘录和证据定位流程，不是真实研究结果。"
        encoded = base64.b64encode(text.encode()).decode()
        imported, _ = task("/api/import", {}, {"files":[{"name":"部署验证.txt", "data":encoded}]})
        assert imported["code"] == 200
        assert len(imported["result"]["corpus"]["documents"]) == 1
        report["checks"].append("Document import over HTTPS")
        generated, identifier = task("/api/generate", imported["result"],
                                     {"topic":"部署验证", "mode":"extract", "operation_id":"public-smoke-test"}, acknowledge=False)
        assert generated["code"] == 200 and generated["progress"]["status"] == "complete"
        state = generated["result"]
        assert state["run"]["topic"] == "部署验证" and state["run"]["claims"]
        report["checks"].append("Background generation, progress and evidence-bearing result")
        empty, _ = task("/api/generate", state, {"topic":"量子纠缠超导", "mode":"extract"})
        assert empty["code"] == 400 and empty["progress"]["status"] == "error"
        assert "没有生成正文" in empty["result"]["error"] and "run" not in empty["result"]
        assert b"emptySavedDraft" in call("/app.js")[1]
        report["checks"].append("Empty drafts rejected; existing manuscripts retained; retry composer deployed")
        assert call("/api/browser/jobs/" + identifier, fresh=True)[0] == 404
        assert call("/api/browser/execute", {}, origin="https://untrusted.invalid")[0] == 403
        assert call("/api/browser/ack", {"job_id":identifier})[0] == 200
        assert call("/api/browser/jobs/" + identifier)[0] == 404
        report["checks"].append("Cross-visitor isolation and cross-origin rejection")
        exported, identifier = task("/api/export", state, acknowledge=False)
        assert exported["binary"]
        status, data, _ = call("/api/browser/result/" + identifier)
        assert status == 200 and data.startswith(b"PK")
        assert call("/api/browser/ack", {"job_id":identifier})[0] == 200
        assert call("/api/browser/result/" + identifier)[0] == 404
        report["checks"].append("Review and evidence ledger export")
        assert json.loads(call("/api/browser/legacy")[1])["threads"] == []
        report["checks"].append("Browser storage mode, no saved server workspace, released task results")
        report["live_ai_generation"] = "Not performed: visitors supply their own paid API keys."
    target = ROOT / "outputs" / "deployment-verification.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    verify()
