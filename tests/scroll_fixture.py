"""Manual browser regression fixture: python -m tests.scroll_fixture.

Uses the real frontend and HTTP handlers with in-memory demo conversations and a
delayed fake provider. No API calls, credentials or saved workspaces are involved.
Inspect body[data-scroll-probe] to see per-frame chat positions during a reply.
"""
import json
import time
from unittest.mock import patch

from web_app import Handler, Server, Session, STATIC


PROBE = """
(() => {
  let probe=null, running=false;
  function frame(){
    const log=document.querySelector("#chatMessages");
    const busy=!!document.querySelector("#liveProcess");
    if(log && busy){
      if(!running)probe={start:log.scrollTop,min:log.scrollTop,max:log.scrollTop,maxGap:0,samples:0};
      probe.min=Math.min(probe.min,log.scrollTop);
      probe.max=Math.max(probe.max,log.scrollTop);
      probe.maxGap=Math.max(probe.maxGap,log.scrollHeight-log.clientHeight-log.scrollTop);
      probe.samples++;
    }
    if(probe && log){
      probe.last=log.scrollTop;probe.running=busy;
      probe.gap=log.scrollHeight-log.clientHeight-log.scrollTop;
      document.body.dataset.scrollProbe=JSON.stringify(probe);
    }
    running=busy;requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
})();
"""


def seed(session, title):
    session.run["mode"] = "model"
    session.run["topic"] = title
    session.run["conversation"] = []
    for i in range(18):
        session.run["conversation"].extend([
            {"role":"user", "content":f"第{i+1}轮：请说明演示材料中的反馈方式。"},
            {"role":"assistant", "content":"演示材料用于测试文献关联和阅读交互，不代表真实研究发现。" * 3},
        ])
    session.commit()


class FixtureHandler(Handler):
    def session(self):
        session = super().session()
        with self.workspace.lock:
            if not getattr(self.workspace, "scroll_seeded", False):
                seed(self.workspace.threads["main"], "对话滚动验证")
                other = Session()
                seed(other, "另一研究会话")
                self.workspace.threads["a" * 24] = other
                self.workspace.scroll_seeded = True
        return session

    def do_GET(self):
        if self.path == "/scroll-probe.js":
            return self.send(PROBE.encode(), "text/javascript; charset=utf-8")
        if self.path == "/":
            content = (STATIC / "index.html").read_text(encoding="utf-8")
            return self.send(content.replace("</body>", '<script src="/scroll-probe.js"></script></body>').encode(),
                             "text/html; charset=utf-8")
        return super().do_GET()


def fake_model(config, path, payload):
    for i in range(8):
        time.sleep(1)
        for workspace in list(server.sessions.values()):
            for session in list(workspace.threads.values()):
                if session.progress_state()["status"] == "running":
                    session.report_progress("reply", f"测试进度 {i+1}：正在检查原文片段与写作上下文。")
    instruction = json.loads(payload["messages"][1]["content"])["instruction"]
    if "模拟错误" in instruction:
        raise ValueError("用于验证滚动位置的模拟错误。")
    output = {"message":"本次为交互测试回复，正文保持不变。" * 8, "operations":[]}
    return {"choices":[{"message":{"content":json.dumps(output)}}]}


if __name__ == "__main__":
    with patch("web_app.load_model_config", return_value={"base":"https://example.invalid","model":"fixture","key":"local-test"}), \
         patch("trace_review.revision.request_model", side_effect=fake_model):
        server = Server(("127.0.0.1", 8766))
        server.RequestHandlerClass = FixtureHandler
        print("Scroll fixture: http://127.0.0.1:8766", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
