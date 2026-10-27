"""A continuous article and its sentence-level audit share the same claim IDs."""
from collections import Counter


def build_article(output, claims, mode):
    identifiers = [claim["id"] for claim in claims]
    raw_sections = output.get("sections")
    if raw_sections is None:
        if mode == "model" and claims:
            raise ValueError("模型只返回了零散陈述，没有返回综述章节和段落，请重新生成。")
        sections = [{"title": "原文摘录", "paragraphs": [identifiers[i:i + 3] for i in range(0, len(identifiers), 3)]}] if claims else []
    else:
        if not isinstance(raw_sections, list):
            raise ValueError("综述章节格式无效。")
        sections, used = [], []
        for section in raw_sections:
            if not isinstance(section, dict) or not isinstance(section.get("title"), str) or not section["title"].strip():
                raise ValueError("综述章节缺少标题。")
            paragraphs = []
            if not isinstance(section.get("paragraphs"), list):
                raise ValueError("综述缺少段落结构。")
            for paragraph in section["paragraphs"]:
                if not isinstance(paragraph, list) or not paragraph:
                    raise ValueError("综述包含空段落或无效段落。")
                if any(type(index) is not int or not 1 <= index <= len(identifiers) for index in paragraph):
                    raise ValueError("综述段落引用了不存在的句子。")
                ids = [identifiers[index - 1] for index in paragraph]
                paragraphs.append(ids)
                used.extend(ids)
            if paragraphs:
                sections.append({"title": section["title"].strip(), "paragraphs": paragraphs})
        if Counter(used) != Counter(identifiers):
            raise ValueError("综述正文未完整覆盖全部陈述，或重复使用了陈述；当前草稿未替换。")
    title = output.get("title")
    return {"title": title.strip() if isinstance(title, str) and title.strip() else "",
            "kind": "review" if mode == "model" else "extract", "sections": sections}


def reference_list(run):
    cited = {e.get("source_id") for claim in run["claims"] for e in claim["evidence"]}
    return [{"number": number, "source_id": doc["id"], "filename": doc["filename"]}
            for number, doc in enumerate((d for d in run["corpus"]["documents"] if d["id"] in cited), 1)]


def article_markdown(run):
    article = run.get("article")
    if article is None:
        return "# " + run["topic"] + "\n\n这份旧账本只有逐句陈述，请重新生成完整综述。\n"
    title = article.get("title") or run["topic"]
    lines = ["# " + title, ""]
    if run["mode"] == "extract":
        lines.extend(["> 离线原文摘录，未调用 AI，不是完整综述。", ""])
    if any("合成演示材料" in page["text"] for doc in run["corpus"]["documents"] for page in doc["pages"]):
        lines.extend(["> 合成材料演示，不可作为真实学术文献或研究结论。", ""])
    claims = {c["id"]: c for c in run["claims"]}
    refs = reference_list(run)
    numbers = {r["source_id"]: r["number"] for r in refs}
    for section in article["sections"]:
        lines.extend(["## " + section["title"], ""])
        for paragraph in section["paragraphs"]:
            text = []
            for identifier in paragraph:
                claim = claims[identifier]
                citations = sorted({numbers[e["source_id"]] for e in claim["evidence"] if e.get("source_id") in numbers})
                value = claim["text"] + "".join(f"[{number}]" for number in citations)
                if not claim["evidence"] or not all(e["valid"] for e in claim["evidence"]):
                    value += "〔证据定位未通过〕"
                text.append(value)
            lines.extend(["".join(text), ""])
    lines.extend(["## 文献来源", ""])
    lines.extend(f"[{r['number']}] {r['filename']}" for r in refs)
    lines.extend(["", "注：以上书目按导入文件名列出；逐句引文、页序、字符位置与人工核验记录见 audit.md。", ""])
    return "\n".join(lines)
