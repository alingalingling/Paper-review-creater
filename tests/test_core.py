import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from trace_review.core import (annotate, check_evidence, export_run, make_run,
                               metrics, model_generate, read_documents, recheck, digest)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / "论文.txt"
        self.source.write_text("反馈研究。\n该研究没有证明反馈能够提高考试成绩。\f长期学习效果尚不明确。", encoding="utf-8")
        self.corpus = read_documents([self.source])
        self.evidence = {"source_id": self.corpus["documents"][0]["id"], "page": 1,
                         "quote": "该研究没有证明反馈能够提高考试成绩。"}

    def test_exact_quote_and_offsets(self):
        e = check_evidence(self.corpus, self.evidence)
        page = self.corpus["documents"][0]["pages"][0]["text"]
        self.assertTrue(e["valid"])
        self.assertEqual(page[e["start"]:e["end"]], self.evidence["quote"])

    def test_fabricated_quote_page_and_source_fail(self):
        for field, value in [("quote", "研究证明反馈提高了考试成绩。"), ("page", 99), ("source_id", "fake"), ("quote", " ")]:
            with self.subTest(field=field, value=value):
                self.assertFalse(check_evidence(self.corpus, {**self.evidence, field: value})["valid"])

    def test_snapshot_tamper_fails(self):
        self.corpus["documents"][0]["pages"][0]["text"] += "被修改"
        self.assertFalse(check_evidence(self.corpus, self.evidence)["valid"])

    def test_pdf_layout_is_restored_to_exact_snapshot_offsets(self):
        text = ("Context.\nthe encoder employs layers of ProbSparse self-\n"
                "attention and distilling to generate hidden represen-\ntations.\n"
                "The decoder predicts the entire future sequence in a single\nforward pass.")
        page = self.corpus["documents"][0]["pages"][0]
        page.update(text=text, text_sha256=digest(text))
        for quote in [
            "the encoder employs layers of ProbSparse self-attention and distilling to generate hidden representations.",
            "The decoder predicts the entire future sequence in a single forward pass.",
        ]:
            with self.subTest(quote=quote):
                result = check_evidence(self.corpus, {**self.evidence, "quote":quote})
                self.assertTrue(result["valid"])
                self.assertEqual(result["quote"], text[result["start"]:result["end"]])
                self.assertEqual(result["submitted_quote"], quote)
                self.assertEqual(result["match_method"], "pdf_layout")
                self.assertTrue(check_evidence(self.corpus, result)["valid"])

    def test_layout_matching_does_not_accept_changed_facts_or_omissions(self):
        text = "Accuracy was not 80%.\nProbSparse\nEncoder\nDistilling\nDecoder\nself-attention"
        page = self.corpus["documents"][0]["pages"][0]
        page.update(text=text, text_sha256=digest(text))
        for quote in ["Accuracy was 80%.", "Accuracy was not 90%.",
                      "ProbSparse Encoder Decoder", "selfattention"]:
            with self.subTest(quote=quote):
                self.assertFalse(check_evidence(self.corpus, {**self.evidence, "quote":quote})["valid"])

    def test_pdf_ligatures_are_mapped_but_math_is_not_rewritten(self):
        text = "The eﬃcient model uses x², not x2."
        page = self.corpus["documents"][0]["pages"][0]
        page.update(text=text, text_sha256=digest(text))
        result = check_evidence(self.corpus, {**self.evidence, "quote":"The efficient model uses x²"})
        self.assertTrue(result["valid"])
        self.assertEqual(result["quote"], "The eﬃcient model uses x²")
        self.assertFalse(check_evidence(self.corpus, {**self.evidence, "quote":"model uses x2"})["valid"])

    def test_repeated_file_is_not_counted_twice(self):
        result = read_documents([self.source, self.source])
        self.assertEqual(len(result["documents"]), 1)
        self.assertTrue(result["warnings"])

    def test_no_relevant_evidence_is_not_success(self):
        run = make_run(self.corpus, "量子计算超导")
        self.assertEqual(run["claims"], [])
        self.assertIsNone(metrics(run)["location_rate"])
        self.assertTrue(run["gaps"])

    def test_extract_is_pending_not_semantically_approved(self):
        run = make_run(self.corpus, "反馈")
        self.assertTrue(run["claims"])
        self.assertEqual(metrics(run)["reviewed"], 0)
        self.assertIsNone(metrics(run)["supported_among_reviewed"])
        self.assertEqual(metrics(run)["confirmed_support_lower_bound"], 0)

    def test_model_contradiction_not_auto_supported(self):
        output = {"claims": [{"text": "反馈能够提高考试成绩。", "evidence": [self.evidence]}], "gaps": [],
                  "sections": [{"title": "研究发现", "paragraphs": [[1]]}]}
        with patch("trace_review.core.model_generate", return_value=(output, {"model": "mock"})):
            run = make_run(self.corpus, "反馈", mode="model")
        self.assertEqual(metrics(run)["located"], 1)
        self.assertEqual(run["claims"][0]["label"], "pending")
        annotate(run["claims"][0], "contradicted", "R1", "将未证明改成了已证明")
        self.assertEqual(metrics(run)["supported_among_reviewed"], 0)

    def test_bad_citations_retained_in_denominator(self):
        output = {"claims": [{"text": "没有证据的句子。", "evidence": []},
                             {"text": self.evidence["quote"], "evidence": [self.evidence]}], "gaps": [],
                  "sections": [{"title": "研究发现", "paragraphs": [[1,2]]}]}
        with patch("trace_review.core.model_generate", return_value=(output, {"model": "mock"})):
            run = make_run(self.corpus, "反馈", mode="model")
        self.assertEqual(metrics(run)["location_rate"], 0.5)
        with self.assertRaises(ValueError):
            annotate(run["claims"][0], "supported", "R1")
        annotate(run["claims"][1], "supported", "R1")
        self.assertEqual(metrics(run)["confirmed_support_lower_bound"], 0.5)

    def test_export_roundtrip_and_history(self):
        run = make_run(self.corpus, "反馈")
        annotate(run["claims"][0], "partial", "R1", "复合命题待拆解")
        annotate(run["claims"][0], "supported", "R2", "已检查全部事实")
        destination = export_run(run, self.folder / "out")
        loaded = recheck(json.loads((destination / "ledger.json").read_text(encoding="utf-8")))
        self.assertEqual(metrics(loaded), metrics(run))
        self.assertEqual(len(loaded["claims"][0]["history"]), 2)
        self.assertTrue((destination / "ledger.csv").exists())
        self.assertTrue((destination / "review.md").exists())

    def test_reviewer_required(self):
        run = make_run(self.corpus, "反馈")
        with self.assertRaises(ValueError):
            annotate(run["claims"][0], "supported", "")

    def test_missing_model_config_does_not_fake_generation(self):
        with self.assertRaises(ValueError):
            model_generate("反馈", [self.evidence], config={})

    def test_model_request_and_response_contract(self):
        result = {"claims": [{"text": "原文陈述。", "evidence": [self.evidence]}], "gaps": []}
        response = {"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}]}
        opener = MagicMock()
        opener.open.return_value = io.BytesIO(json.dumps(response).encode("utf-8"))
        environment = {"TRACE_API_BASE": "https://example.invalid/v1", "TRACE_MODEL": "mock-model", "TRACE_API_KEY": "fake-test-key"}
        with patch.dict("os.environ", environment), patch("urllib.request.build_opener", return_value=opener):
            output, metadata = model_generate("反馈", [self.evidence])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.invalid/v1/chat/completions")
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "mock-model")
        self.assertIn(self.evidence["quote"], payload["messages"][1]["content"])
        self.assertEqual(output, result)
        self.assertNotIn("fake-test-key", json.dumps(metadata))

    def test_unsafe_or_credential_bearing_model_url_rejected(self):
        for url in ("http://example.invalid/v1", "https://user:pass@example.invalid/v1", "https://example.invalid/v1?key=secret"):
            with self.subTest(url=url), patch.dict("os.environ", {"TRACE_API_BASE": url, "TRACE_MODEL": "m", "TRACE_API_KEY": "k"}):
                with self.assertRaises(ValueError):
                    model_generate("反馈", [self.evidence])

    def test_malformed_model_output_rejected(self):
        with patch("trace_review.core.model_generate", return_value=({"claims": "文字", "gaps": []}, {})):
            with self.assertRaises(ValueError):
                make_run(self.corpus, "反馈", "model")

    def test_deepseek_json_mode_and_receipt(self):
        output = {"claims": [{"text": "反馈研究。", "evidence": [self.evidence]}], "gaps": []}
        response = {"id": "test-receipt", "model": "deepseek-flash", "usage": {"total_tokens": 123},
                    "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(output)}}]}
        with patch("trace_review.core.request_model", return_value=response) as request:
            generated, metadata = model_generate("反馈", [self.evidence],
                {"base": "https://api.deepseek.com", "model": "deepseek-flash", "key": "fake-test-key"})
        payload = request.call_args.args[2]
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(metadata["request_id"], "test-receipt")
        self.assertEqual(metadata["usage"]["total_tokens"], 123)

    def test_balance_error_is_specific_and_does_not_echo_provider_body(self):
        error = urllib.error.HTTPError("https://api.deepseek.com/chat/completions", 402, "payment", {}, io.BytesIO(b"fake-private-key"))
        opener = MagicMock()
        opener.open.side_effect = error
        with patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(ValueError, "余额不足") as raised:
                model_generate("反馈", [self.evidence], {"base": "https://api.deepseek.com", "model": "deepseek-flash", "key": "fake-private-key"})
        self.assertNotIn("fake-private-key", str(raised.exception))

    def test_truncated_generation_not_presented_as_valid_draft(self):
        response = {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}
        with patch("trace_review.core.request_model", return_value=response):
            with self.assertRaisesRegex(ValueError, "长度上限"):
                model_generate("反馈", [self.evidence], {"base": "https://api.deepseek.com", "model": "deepseek-flash", "key": "fake"})


if __name__ == "__main__":
    unittest.main()
