"""Local presentation check with an intentionally slow, synthetic workspace."""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from web_app import Handler, Server, Session
from trace_review.core import digest, make_run

if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--delay", type=float, default=0)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--empty-review", action="store_true", help="Show a legacy zero-word draft for UI regression checks")
    args=parser.parse_args()
    class PresentationHandler(Handler):
        def do_GET(self):
            if self.path=="/api/bootstrap":
                time.sleep(args.delay)
                if args.empty_review:
                    session=Session(public=True, transient=True)
                    texts=["Synthetic example: soil and water conservation is discussed here.",
                           "Synthetic example: runoff and soil erosion are described here."]
                    session.corpus={"documents":[{"id":f"fixture-{i}", "filename":f"synthetic-paper-{i}.txt",
                                    "pages":[{"number":1,"text":text,"text_sha256":digest(text)}]}
                                   for i,text in enumerate(texts)],"warnings":[]}
                    session.run=make_run(session.corpus,"水土保持相关内容","extract")
                    session.run.update(mode="model",requirements="随意",conversation=[
                        {"role":"user","content":"主题：水土保持相关内容\n要求：随意"},
                        {"role":"assistant","content":"已生成《水土保持相关内容》，正文 0 字。","document_ready":True}])
                    return self.send({"browser_storage":True,"sample":session.state()})
            return super().do_GET()
    server=Server(("127.0.0.1",args.port),origin="https://presentation-test.invalid")
    server.RequestHandlerClass=PresentationHandler
    print(f"Presentation preview: http://127.0.0.1:{args.port}",flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
