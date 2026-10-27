"""旧版 tkinter 界面，仅作兼容保留；默认入口为浏览器工作台。"""
import json
import os
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from trace_review.core import LABELS, annotate, export_run, located, make_run, metrics, read_documents, recheck

ROOT = Path(__file__).resolve().parent


class App:
    def __init__(self, root):
        self.root, self.corpus, self.run = root, None, None
        self.dirty = False
        self.events = queue.Queue()
        root.title("循证综述 · 每句话都有证据账本")
        root.geometry("1220x800")
        root.minsize(950, 650)
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TButton", padding=7)
        style.configure("Treeview", rowheight=34, font=("Microsoft YaHei UI", 10))
        outer = ttk.Frame(root, padding=20)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="循证综述", font=("Microsoft YaHei UI", 24, "bold")).pack(anchor="w")
        ttk.Label(outer, text="限定文献内写作 → 逐句定位 → 人工核验 → 证据账本", padding=(0, 6, 0, 12)).pack(anchor="w")
        bar = ttk.Frame(outer)
        bar.pack(fill="x")
        self.busy_controls = []
        for text, callback in [("加载演示材料", self.demo), ("导入文献", self.import_files), ("模型配置", self.settings), ("打开已有账本", self.open_run)]:
            button = ttk.Button(bar, text=text, command=callback)
            button.pack(side="left", padx=(0, 8))
            self.busy_controls.append(button)
        self.source_label = ttk.Label(bar, text="尚未加载文献")
        self.source_label.pack(side="left")
        query = ttk.Frame(outer, padding=(0, 16, 0, 12))
        query.pack(fill="x")
        ttk.Label(query, text="综述主题").pack(side="left")
        self.topic = tk.StringVar(value="反馈 学习效果")
        ttk.Entry(query, textvariable=self.topic, width=36).pack(side="left", padx=8, fill="x", expand=True)
        self.mode = tk.StringVar(value="extract")
        ttk.Radiobutton(query, text="离线摘录演示", variable=self.mode, value="extract").pack(side="left")
        ttk.Radiobutton(query, text="真实 AI", variable=self.mode, value="model").pack(side="left", padx=8)
        self.generate_button = ttk.Button(query, text="生成待核验草稿", command=self.generate)
        self.generate_button.pack(side="left")
        self.busy_controls.append(self.generate_button)
        self.status = tk.StringVar(value="离线演示不调用模型；自动定位不会判定语义支持。")
        ttk.Label(outer, textvariable=self.status, wraplength=1100, padding=(0, 0, 0, 12)).pack(anchor="w")
        split = ttk.Panedwindow(outer, orient="horizontal")
        split.pack(fill="both", expand=True)
        left, right = ttk.Frame(split), ttk.Frame(split, padding=(15, 0, 0, 0))
        split.add(left, weight=3)
        split.add(right, weight=4)
        ttk.Label(left, text="草稿句子 · 选择后查看原文", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", pady=(0, 8))
        self.tree = ttk.Treeview(left, columns=("label", "text"), show="headings", selectmode="browse")
        self.tree.heading("label", text="状态")
        self.tree.heading("text", text="陈述")
        self.tree.column("label", width=90, stretch=False)
        self.tree.column("text", width=350)
        scrollbar = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.show_claim)
        ttk.Label(right, text="原文与核验记录", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", pady=(0, 8))
        self.evidence = tk.Text(right, wrap="word", font=("Microsoft YaHei UI", 11), padx=12, pady=10, height=15)
        self.evidence.pack(fill="both", expand=True)
        self.evidence.tag_configure("quote", background="#e4f0df")
        self.evidence.configure(state="disabled")
        form = ttk.Frame(right, padding=(0, 12, 0, 0))
        form.pack(fill="x")
        self.label = tk.StringVar(value="待核验")
        ttk.Combobox(form, textvariable=self.label, values=list(LABELS.values()), state="readonly", width=14).pack(side="left")
        ttk.Label(form, text="核验者").pack(side="left", padx=8)
        self.reviewer = tk.StringVar()
        ttk.Entry(form, textvariable=self.reviewer, width=18).pack(side="left")
        ttk.Button(form, text="保存判定", command=self.save_label).pack(side="right")
        ttk.Label(right, text="核验备注（范围、数字、因果、复合命题等）").pack(anchor="w", pady=(12, 4))
        self.note = tk.Text(right, height=3, wrap="word", font=("Microsoft YaHei UI", 10))
        self.note.pack(fill="x")
        footer = ttk.Frame(outer, padding=(0, 14, 0, 0))
        footer.pack(fill="x")
        ttk.Label(footer, text="判定先保存在内存中，请导出账本后再关闭。", foreground="#775030").pack(side="left")
        ttk.Button(footer, text="导出草稿 + JSON + CSV", command=self.export).pack(side="right")
        root.after(150, self.poll)
        root.protocol("WM_DELETE_WINDOW", self.close)

    def close(self):
        if self.dirty and not messagebox.askyesno("关闭项目", "当前账本有未导出的内容。现在关闭并放弃这些内容？"):
            return
        self.root.destroy()

    def allow_replace(self):
        return not self.dirty or messagebox.askyesno("替换当前账本", "当前账本有未导出的内容。继续会替换这些内容，是否继续？")

    def load(self, paths):
        if not self.allow_replace():
            return
        try:
            corpus = read_documents(paths)
            self.corpus = corpus
            self.run = None
            self.dirty = False
            self.tree.delete(*self.tree.get_children())
            self.set_evidence("已加载新文献，请生成新的待核验草稿。")
            self.source_label.config(text=f"{len(corpus['documents'])} 篇文献")
            self.status.set("；".join(corpus["warnings"]) or "文献已载入；页序从 1 开始，TXT 按换页符分段。")
        except Exception as exc:
            messagebox.showerror("导入失败", str(exc))

    def demo(self):
        self.load(sorted((ROOT / "data/demo").glob("*.txt")))

    def settings(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("模型配置")
        dialog.geometry("580x320")
        dialog.transient(self.root)
        dialog.grab_set()
        panel = ttk.Frame(dialog, padding=20)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="使用兼容 Chat Completions 的接口；配置仅在当前进程内保留。", wraplength=520).pack(anchor="w", pady=(0, 12))
        fields = {}
        for name, title, default in [("TRACE_API_BASE", "API 基础地址（例如 https://服务商域名/v1）", ""),
                                     ("TRACE_MODEL", "模型名称（按服务商填写）", ""),
                                     ("TRACE_API_KEY", "API Key", "")]:
            ttk.Label(panel, text=title).pack(anchor="w")
            variable = tk.StringVar(value=os.environ.get(name, default))
            ttk.Entry(panel, textvariable=variable, show="*" if name == "TRACE_API_KEY" else "", width=72).pack(fill="x", pady=(2, 10))
            fields[name] = variable
        def save():
            for name, variable in fields.items():
                os.environ[name] = variable.get().strip()
            dialog.destroy()
            self.status.set("模型配置已保存在本次会话内，重启程序后需重新填写。")
        ttk.Button(panel, text="保存到本次会话", command=save).pack(anchor="e")

    def import_files(self):
        paths = filedialog.askopenfilenames(filetypes=[("文献", "*.pdf *.txt *.md")])
        if paths:
            self.load(paths)

    def open_run(self):
        path = filedialog.askopenfilename(filetypes=[("证据账本", "*.json")])
        if not path:
            return
        if not self.allow_replace():
            return
        try:
            run = json.loads(Path(path).read_text(encoding="utf-8"))
            recheck(run)
            self.run, self.corpus = run, run["corpus"]
            self.dirty = False
            self.topic.set(run["topic"])
            self.source_label.config(text=f"{len(self.corpus['documents'])} 篇文献（账本快照）")
            self.refresh()
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))

    def generate(self):
        if not self.corpus:
            messagebox.showinfo("先加载文献", "请导入文献或加载演示材料。")
            return
        if not self.allow_replace():
            return
        mode, corpus, topic = self.mode.get(), self.corpus, self.topic.get()
        if mode == "model" and not messagebox.askyesno("向模型发送证据", "将向 TRACE_API_BASE 配置的模型服务发送主题和检索到的原文片段，并可能产生接口费用。继续？"):
            return
        for button in self.busy_controls:
            button.config(state="disabled")
        self.status.set("正在生成；AI 模式可能需要约一分钟……")
        def worker():
            try:
                self.events.put(("done", make_run(corpus, topic, mode)))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            event, payload = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            for button in self.busy_controls:
                button.config(state="normal")
            if event == "done":
                self.run = payload
                self.dirty = True
                self.refresh()
            else:
                self.status.set("生成失败；已有账本仍可查看。")
                messagebox.showerror("生成失败", payload)
        self.root.after(150, self.poll)

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for c in self.run["claims"]:
            label = LABELS[c["label"]] if located(c) else "定位未通过"
            self.tree.insert("", "end", iid=c["id"], values=(label, c["text"]))
        m = metrics(self.run)
        mode = "离线摘录，未调用 AI" if self.run["mode"] == "extract" else "AI 草稿"
        self.status.set(f"{mode} ｜ 陈述 {m['total']} ｜ 全部引文可定位 {m['located']} ｜ 人工核验 {m['reviewed']} ｜ 充分支持 {m['supported']} ｜ " + "；".join(self.run["gaps"]))
        if self.run["claims"]:
            self.tree.selection_set(self.run["claims"][0]["id"])
            self.show_claim()
        else:
            self.set_evidence("未生成陈述。\n\n" + "\n".join(self.run["gaps"]))

    def current(self):
        selected = self.tree.selection()
        return next((c for c in self.run["claims"] if selected and c["id"] == selected[0]), None) if self.run else None

    def set_evidence(self, text):
        self.evidence.config(state="normal")
        self.evidence.delete("1.0", "end")
        self.evidence.insert("end", text)
        self.evidence.config(state="disabled")

    def show_claim(self, event=None):
        claim = self.current()
        if not claim:
            return
        self.set_evidence(f"{claim['id']}  {claim['text']}\n\n自动定位通过也需要核验对象、范围、数字和否定条件。\n\n")
        self.evidence.config(state="normal")
        for e in claim["evidence"]:
            self.evidence.insert("end", f"{e.get('filename', '未知来源')} · 页序 {e.get('page')} · 字符 [{e.get('start')}, {e.get('end')})\n{e['reason']}\n")
            doc = next((d for d in self.corpus["documents"] if d["id"] == e.get("source_id")), None)
            page = next((p for p in doc["pages"] if p["number"] == e.get("page")), None) if doc else None
            if e["valid"] and page:
                text, start, end = page["text"], e["start"], e["end"]
                self.evidence.insert("end", text[max(0, start-120):start])
                self.evidence.insert("end", text[start:end], "quote")
                self.evidence.insert("end", text[end:end+120] + "\n\n")
            else:
                self.evidence.insert("end", str(e.get("quote", "")) + "\n\n")
        if not claim["evidence"]:
            self.evidence.insert("end", "此陈述没有引用证据。")
        self.evidence.config(state="disabled")
        self.label.set(LABELS[claim["label"]])
        self.reviewer.set(claim["reviewer"])
        self.note.delete("1.0", "end")
        self.note.insert("1.0", claim["note"])

    def save_label(self):
        claim = self.current()
        if not claim:
            return
        try:
            annotate(claim, next(k for k, v in LABELS.items() if v == self.label.get()),
                     self.reviewer.get(), self.note.get("1.0", "end").strip())
            self.dirty = True
            selected = claim["id"]
            self.refresh()
            self.tree.selection_set(selected)
            self.show_claim()
        except ValueError as exc:
            messagebox.showerror("无法保存", str(exc))

    def export(self):
        if not self.run:
            messagebox.showinfo("没有账本", "请先生成草稿。")
            return
        folder = filedialog.askdirectory(title="选择导出父目录，将创建独立时间戳文件夹")
        if folder:
            destination = Path(folder) / ("循证账本_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
            try:
                export_run(self.run, destination)
                self.dirty = False
                self.status.set(f"已导出：{destination}")
            except Exception as exc:
                messagebox.showerror("导出失败", str(exc))


if __name__ == "__main__":
    window = tk.Tk()
    App(window)
    window.mainloop()
