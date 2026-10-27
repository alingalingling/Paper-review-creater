import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trace_review.article import build_article, article_markdown
from trace_review.core import make_run, read_documents, export_run


class ArticleTests(unittest.TestCase):
    def setUp(self):
        self.claims = [{"id": "S001"}, {"id": "S002"}, {"id": "S003"}]

    def test_paragraphs_resolve_to_same_audited_sentences(self):
        result = build_article({"title": "研究综述", "sections": [
            {"title": "研究范围", "paragraphs": [[1,2]]},
            {"title": "结论", "paragraphs": [[3]]}]}, self.claims, "model")
        self.assertEqual(result["sections"][0]["paragraphs"], [["S001", "S002"]])

    def test_omitted_duplicate_or_unknown_sentences_rejected(self):
        for indexes in ([1,2], [1,2,2,3], [1,2,4], [1,2,True]):
            with self.subTest(indexes=indexes), self.assertRaises(ValueError):
                build_article({"sections":[{"title":"章节", "paragraphs":[indexes]}]}, self.claims, "model")

    def test_model_sentence_list_is_not_a_complete_review(self):
        with self.assertRaisesRegex(ValueError, "零散陈述"):
            build_article({}, self.claims, "model")

    def test_article_and_audit_export_have_distinct_purposes(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "来源.txt"
            quote = "反馈研究只记录短期练习完成率，没有记录长期成绩。"
            source.write_text(quote, encoding="utf-8")
            corpus = read_documents([source])
            evidence = {"source_id":corpus["documents"][0]["id"], "page":1, "quote":quote}
            output = {"title":"反馈研究综述", "claims":[{"text":quote,"evidence":[evidence]}],
                      "sections":[{"title":"研究结论与局限", "paragraphs":[[1]]}], "gaps":[]}
            with patch("trace_review.core.model_generate",return_value=(output,{"model":"mock"})):
                run = make_run(corpus,"反馈","model")
            target = export_run(run,Path(folder)/"out")
            review = (target/"review.md").read_text(encoding="utf-8")
            self.assertIn("## 研究结论与局限",review)
            self.assertIn(quote+"[1]",review)
            self.assertNotIn("## S001",review)
            self.assertIn("## S001",(target/"audit.md").read_text(encoding="utf-8"))
            self.assertEqual(len(run["claims"]),1)
