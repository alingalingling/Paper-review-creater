"""Bounds and isolated model configuration for the public tunnel deployment."""
from collections import defaultdict, deque
import threading
import time
from urllib.parse import urlparse

from .config import DEFAULT_BASE, DEFAULT_MODEL


def visitor_config():
    # Never consult the owner's environment or encrypted desktop credentials.
    return {"base": DEFAULT_BASE, "model": DEFAULT_MODEL, "key": ""}


def public_origin(value):
    parsed = urlparse(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
            parsed.password or parsed.port not in (None, 443) or
            parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("公开地址必须是 HTTPS 域名，例如 https://review.njfu.org")
    return "https://" + parsed.netloc.lower()


def visitor_settings(body, previous):
    base = str(body.get("base") or DEFAULT_BASE).strip().rstrip("/")
    if base not in (DEFAULT_BASE, DEFAULT_BASE + "/v1"):
        raise ValueError("公开站仅连接 DeepSeek 官方接口。")
    model = str(body.get("model") or DEFAULT_MODEL).strip()
    if len(model) > 100 or not model.isascii() or any(c.isspace() for c in model):
        raise ValueError("请填写有效的 DeepSeek 模型名称。")
    key = str(body.get("key", "")).strip() or previous["key"]
    if body.get("clear_key"):
        key = ""
    if len(key) > 512 or any(c.isspace() for c in key):
        raise ValueError("API Key 格式不正确。")
    return {"base": base, "model": model, "key": key}


def check_corpus(corpus):
    documents = corpus.get("documents", [])
    if len(documents) > 40:
        raise ValueError("每个研究最多保留 40 篇文献，请新建研究继续。")
    if sum(len(p["text"]) for d in documents for p in d["pages"]) > 4_000_000:
        raise ValueError("本次研究的文献内容过多，请分成几个研究导入。")


class PublicLimits:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests = defaultdict(deque)
        self.jobs = threading.BoundedSemaphore(2)

    def allow(self, client, category, maximum, seconds):
        now = time.monotonic()
        with self.lock:
            # Keep the limiter itself bounded for a public, single-PC prototype.
            if len(self.requests) > 4096:
                self.requests = defaultdict(deque, {
                    key: times for key, times in self.requests.items()
                    if times and now - times[-1] < 3600
                })
                if len(self.requests) > 4096:
                    return False
            times = self.requests[(client, category)]
            while times and times[0] <= now - seconds:
                times.popleft()
            if len(times) >= maximum:
                return False
            times.append(now)
            return True
