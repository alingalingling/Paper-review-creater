"""Bounded, short-lived processing for browser-owned manuscripts."""
import secrets
import threading
import time


class BrowserJobs:
    def __init__(self, retention=300, capacity=4):
        self.retention = retention
        self.capacity = capacity
        self.lock = threading.RLock()
        self.items = {}

    def prune(self):
        now = time.monotonic()
        with self.lock:
            for key, item in list(self.items.items()):
                if item["finished"] and now - item["finished"] >= self.retention:
                    del self.items[key]

    def add(self, owner, session, identifier=None):
        with self.lock:
            self.prune()
            if len(self.items) >= self.capacity:
                raise ValueError("临时处理空间正忙，请稍后再试。")
            identifier = identifier or secrets.token_hex(16)
            if identifier in self.items:
                raise ValueError("此任务已提交，请查看现有处理进度。")
            self.items[identifier] = {"owner":owner, "session":session, "finished":0,
                                      "result":None, "code":202, "mime":None}
            return identifier

    def finish(self, identifier, result, code, mime):
        with self.lock:
            item = self.items[identifier]
            session = item["session"]
            session.background_job = False
            item["progress"] = session.progress_state()
            if code >= 400:
                item["progress"]["status"] = "error"
            else:
                item["progress"]["status"] = "complete"
            session.config["key"] = ""
            # Drop input documents and credentials; keep only the result until ACK/expiry.
            item.update(session=None, result=result, code=code, mime=mime, finished=time.monotonic())

    def get(self, owner, identifier):
        with self.lock:
            self.prune()
            item = self.items.get(identifier)
            if not item or item["owner"] != owner:
                return None
            if item["session"] is not None:
                return {"progress":item["session"].progress_state(), "ready":False}
            return {"progress":item["progress"], "ready":True, "result":item["result"],
                    "code":item["code"], "mime":item["mime"]}

    def acknowledge(self, owner, identifier):
        with self.lock:
            item = self.items.get(identifier)
            if item and item["owner"] == owner and item["finished"]:
                del self.items[identifier]
