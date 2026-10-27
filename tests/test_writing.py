import json
import unittest
from unittest.mock import patch

from trace_review.core import make_run, model_generate, digest
from trace_review.writing import length_constraints, assess_writing


class WritingTests(unittest.TestCase):
    def setUp(self):
        self.quote = "研究比较两种反馈方式，并记录练习完成情况，但未测量考试成绩。"
        self.corpus = {"documents": [{"id": "source", "filename": "研究.txt", "pages": [
            {"number": 1, "text": self.quote, "text_sha256": digest(self.quote)}]}], "warnings": []}
        self.evidence = {"source_id": "source", "page": 1, "quote": self.quote}

    def output(self, text):
        return {"title": "反馈研究", "claims": [{"text": text, "evidence": [self.evidence]}],
                "sections": [{"title": "研究发现", "paragraphs": [[1]]}], "gaps": []}

    def test_chinese_arabic_limits_and_negated_maximum(self):
        for text, expected in [
            ("不少于八百字", {"min_characters": 800}),
            ("不要少于800字", {"min_characters": 800}),
            ("不超过八百字", {"max_characters": 800}),
            ("不要超过800字", {"max_characters": 800}),
            ("一千二百字以上", {"min_characters": 1200}),
            ("800至1000字", {"min_characters": 800, "max_characters": 1000}),
            ("约八百字", {"min_characters": 720, "max_characters": 880, "target_characters": 800}),
        ]:
            with self.subTest(text=text):
                self.assertEqual(length_constraints("反馈", text), expected)

    def test_explicit_requirements_override_topic_length(self):
        self.assertEqual(length_constraints("反馈，不少于1000字", "不超过八百字"), {"max_characters":800})
        self.assertEqual(length_constraints("反馈", "写800字"),
                         {"min_characters":720,"max_characters":880,"target_characters":800})

    def test_overlong_draft_is_shortened_automatically(self):
        with patch("trace_review.core.model_generate", side_effect=[
            (self.output(self.quote), {"model":"mock"}), (self.output("反馈。"), {"model":"mock"})]):
            run=make_run(self.corpus, "反馈", "model", requirements="不超过10字")
        self.assertTrue(run["writing"]["met"])
        self.assertLessEqual(run["writing"]["character_count"],10)
        self.assertEqual(run["generation"]["attempts"],2)

    def test_conflicting_limits_are_actionable(self):
        with self.assertRaisesRegex(ValueError, "上下限"):
            length_constraints("反馈", "不少于1000字，不超过800字")

    def test_count_excludes_punctuation_and_spaces(self):
        result = assess_writing([{"text": "研究，反馈。 A1\n"}], {"min_characters": 7})
        self.assertEqual(result["character_count"], 6)
        self.assertFalse(result["met"])

    def test_short_draft_is_expanded_automatically_and_requirements_persist(self):
        with patch("trace_review.core.model_generate", side_effect=[
            (self.output("反馈。"), {"model": "mock"}),
            (self.output(self.quote), {"model": "mock"})]) as generate:
            run = make_run(self.corpus, "反馈", "model", requirements="不少于20字，保留研究局限")
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(generate.call_args_list[0].kwargs["requirements"], run["requirements"])
        self.assertIn("feedback", generate.call_args.kwargs)
        self.assertIn("previous_draft", generate.call_args.kwargs["feedback"])
        self.assertEqual(run["generation"]["attempts"], 2)
        self.assertTrue(run["writing"]["met"])
        self.assertTrue(run["claims"][0]["evidence"][0]["valid"])
        self.assertEqual(run["claims"][0]["label"], "pending")

    def test_bad_article_indexes_retry_without_losing_citations(self):
        bad = self.output(self.quote)
        bad["sections"][0]["paragraphs"] = [[1,1]]
        with patch("trace_review.core.model_generate", side_effect=[
            (bad, {"model": "mock"}), (self.output(self.quote), {"model": "mock"})]):
            run = make_run(self.corpus, "反馈", "model")
        self.assertEqual(run["article"]["sections"][0]["paragraphs"], [["S001"]])
        self.assertEqual(run["generation"]["attempts"], 2)
        self.assertTrue(run["claims"][0]["evidence"][0]["valid"])

    def test_unmet_length_is_visible_not_reported_as_met(self):
        with patch("trace_review.core.model_generate", return_value=(self.output(self.quote), {"model": "mock"})) as generate:
            run = make_run(self.corpus, "反馈，不少于八百字", "model")
        self.assertEqual(generate.call_count, 3)
        self.assertFalse(run["writing"]["met"])
        self.assertIn("800", run["writing"]["issues"][0])
        self.assertTrue(run["article"]["sections"])
        self.assertIn(run["writing"]["issues"][0], run["gaps"])

    def test_invalid_json_is_automatically_regenerated(self):
        responses = [
            {"choices":[{"message":{"content":"not json"}}]},
            {"choices":[{"message":{"content":json.dumps(self.output(self.quote))}}]},
        ]
        with patch("trace_review.core.request_model",side_effect=responses) as request:
            run=make_run(self.corpus,"反馈","model",model_config={"base":"https://api.deepseek.com","model":"mock","key":"test"})
        self.assertEqual(request.call_count,2)
        self.assertEqual(run["generation"]["attempts"],2)
        self.assertTrue(run["claims"][0]["evidence"][0]["valid"])

    def test_configuration_failures_do_not_repeat_model_calls(self):
        with patch("trace_review.core.model_generate", side_effect=ValueError("请配置模型")) as generate:
            with self.assertRaisesRegex(ValueError, "配置"):
                make_run(self.corpus, "反馈", "model", requirements="800字")
        generate.assert_called_once()

    def test_requirements_are_sent_separately_from_topic_and_evidence(self):
        response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(self.output(self.quote))}}]}
        with patch("trace_review.core.request_model", return_value=response) as request:
            model_generate("反馈", [self.evidence], {"base":"https://api.deepseek.com", "model":"mock", "key":"test"},
                           requirements="不超过八百字，比较研究局限")
        payload = request.call_args.args[2]
        user = json.loads(payload["messages"][1]["content"])
        self.assertEqual(user["topic"], "反馈")
        self.assertEqual(user["length_constraints"], {"max_characters": 800})
        self.assertEqual(user["requirements"], "不超过八百字，比较研究局限")
        self.assertEqual(user["evidence"], [self.evidence])


if __name__ == "__main__":
    unittest.main()
