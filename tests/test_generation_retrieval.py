import json
import unittest
from unittest.mock import Mock, patch

from trace_review.core import check_evidence, digest, make_run, model_search_terms, tokens
from trace_review.evidence_context import generation_evidence


def corpus_for(*texts):
    return {"documents":[{"id":f"doc-{i}", "filename":f"paper-{i}.txt",
             "pages":[{"number":1,"text":text,"text_sha256":digest(text)}]}
            for i, text in enumerate(texts)], "warnings":[]}


def output_for(evidence, text="该合成材料介绍了土壤与水资源保护。"):
    return {"title":"合成材料测试", "claims":[{"text":text,"evidence":[evidence]}],
            "sections":[{"title":"内容","paragraphs":[[1]]}], "gaps":[]}


class GenerationRetrievalTests(unittest.TestCase):
    def test_chinese_topic_reads_both_english_papers_without_literal_matches(self):
        corpus = corpus_for("Synthetic example: soil and water conservation is discussed here.",
                            "Synthetic example: runoff and erosion are discussed in this paragraph.")
        captured = []
        def generate(topic, candidates, **kwargs):
            captured.extend(candidates)
            return output_for(candidates[0]), {"model":"test"}
        with patch("trace_review.core.model_generate", side_effect=generate), \
             patch("trace_review.core.model_search_terms") as expand:
            run = make_run(corpus, "水土保持相关内容", "model", requirements="随意")
        expand.assert_not_called()  # All text fits; translation need not add a paid request.
        self.assertEqual({e["source_id"] for e in captured}, {"doc-0", "doc-1"})
        self.assertGreater(run["writing"]["character_count"], 0)
        self.assertTrue(run["claims"][0]["evidence"][0]["valid"])
        self.assertEqual(run["generation"]["retrieval"]["strategy"], "full_context")

    def test_long_corpus_uses_translated_terms_to_reach_later_passages_in_multiple_domains(self):
        for topic, phrase, translated in [
            ("水土保持", "Soil and water conservation reduces soil erosion in this synthetic example.",
             ["soil and water conservation", "soil erosion"]),
            ("电池热管理", "Battery thermal management uses a cooling system in this synthetic example.",
             ["battery thermal management", "cooling system"]),
        ]:
            with self.subTest(topic=topic):
                corpus = corpus_for(("Unrelated instrument calibration and sampling details.\n" * 800) + phrase,
                                    "A second synthetic document describes the sampling protocol.")
                expand = Mock(return_value=translated)
                passages, details = generation_evidence(corpus, topic, expand_query=expand, budget=1800)
                expand.assert_called_once()
                self.assertTrue(any(phrase in e["quote"] for e in passages))
                self.assertEqual({e["source_id"] for e in passages}, {"doc-0", "doc-1"})
                self.assertLessEqual(sum(len(e["quote"]) for e in passages), 1800)
                self.assertEqual(details["search_terms"], translated)
                self.assertTrue(all(check_evidence(corpus, e)["valid"] for e in passages))

    def test_long_chinese_request_calls_translation_then_writing_with_real_passages(self):
        target = "Soil and water conservation is discussed only at the end of this synthetic text."
        corpus = corpus_for("Instrument calibration details.\n" * 1600 + target)
        def generate(topic, candidates, **kwargs):
            evidence = next(e for e in candidates if target in e["quote"])
            return output_for(evidence), {"model":"test"}
        with patch("trace_review.core.model_search_terms", return_value=["soil and water conservation"]) as expand, \
             patch("trace_review.core.model_generate", side_effect=generate):
            run = make_run(corpus, "水土保持", "model", requirements="比较研究方法")
        expand.assert_called_once_with("水土保持", "比较研究方法", None)
        self.assertTrue(run["claims"][0]["evidence"][0]["valid"])
        self.assertLessEqual(run["generation"]["retrieval"]["provided_characters"],36000)

    def test_translation_contract_is_bounded_and_never_becomes_source_evidence(self):
        response = {"choices":[{"message":{"content":json.dumps({
            "terms":["soil conservation","soil conservation",42,"", "x" * 101, "water conservation"]})}}]}
        config = {"base":"https://api.deepseek.com","model":"test","key":"test-only"}
        with patch("trace_review.core.request_model",return_value=response) as request:
            terms = model_search_terms("水土保持", "比较方法", config)
        self.assertEqual(terms,["soil conservation","water conservation"])
        self.assertEqual(request.call_args.args[0],config)
        payload = request.call_args.args[2]
        self.assertEqual(json.loads(payload["messages"][1]["content"])["topic"],"水土保持")
        with patch("trace_review.core.request_model",return_value={"choices":[]}):
            self.assertEqual(model_search_terms("主题", config=config),[])

    def test_zero_matches_and_failed_term_expansion_still_read_real_context(self):
        corpus = corpus_for("English methods and results.\n" * 100)
        passages, details = generation_evidence(corpus, "水土保持", expand_query=lambda:[], budget=1800)
        self.assertTrue(passages)
        self.assertLessEqual(details["provided_characters"],1800)
        self.assertTrue(all(check_evidence(corpus,e)["valid"] for e in passages))

    def test_empty_model_output_is_retried_and_cannot_be_success(self):
        corpus = corpus_for("Synthetic soil and water conservation example.")
        empty = {"claims":[],"gaps":["未识别主题"],"sections":[]}
        with patch("trace_review.core.model_generate",return_value=(empty,{"model":"test"})) as generate:
            with self.assertRaisesRegex(ValueError,"未生成可用正文"):
                make_run(corpus,"水土保持","model")
        self.assertEqual(generate.call_count,3)
        self.assertIn("跨语言",generate.call_args.kwargs["feedback"]["instruction"])

    def test_empty_first_reply_recovers_to_a_cited_review(self):
        corpus = corpus_for("Synthetic soil and water conservation example.")
        evidence = {"source_id":"doc-0","page":1,"quote":corpus["documents"][0]["pages"][0]["text"]}
        with patch("trace_review.core.model_generate",side_effect=[
            ({"claims":[],"gaps":[]},{"model":"test"}),
            (output_for(evidence),{"model":"test"})]):
            run=make_run(corpus,"水土保持","model")
        self.assertEqual(run["generation"]["attempts"],2)
        self.assertGreater(run["writing"]["character_count"],0)

    def test_unreadable_pages_do_not_call_ai(self):
        with patch("trace_review.core.model_generate") as generate:
            with self.assertRaisesRegex(ValueError,"没有可读取的文字"):
                make_run(corpus_for("  \n"),"水土保持","model")
        generate.assert_not_called()

    def test_pdf_spacing_is_normalized_only_for_matching(self):
        self.assertIn("保持",tokens("水 土 保 持"))
        self.assertIn("conservation",tokens("conser-\nvation"))
        text="水 土 保 持\nsoil and water conser-\nvation"
        corpus=corpus_for(text)
        passages,_=generation_evidence(corpus,"水土保持")
        self.assertEqual(passages[0]["quote"],text)
        self.assertTrue(check_evidence(corpus,passages[0])["valid"])


if __name__ == "__main__":
    unittest.main()
