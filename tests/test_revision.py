import copy
import json
import unittest
from unittest.mock import patch

from trace_review.core import digest
from trace_review.revision import (addition_constraints, addition_count, apply_patch,
                                   edit_payload, revise_run, undo_revision)


class RevisionTests(unittest.TestCase):
    def setUp(self):
        text="即时反馈组完成率为80%。延迟反馈组完成率为65%。演示没有测量长期效果。"
        self.evidence={"source_id":"doc","page":1,"quote":"演示没有测量长期效果。"}
        self.run={"topic":"反馈","requirements":"不少于20字","mode":"model","gaps":[],
            "corpus":{"documents":[{"id":"doc","filename":"演示.txt","pages":[{"number":1,"text":text,"text_sha256":digest(text)}]}],"warnings":[]},
            "claims":[{"id":f"S{i:03}","text":sentence,"evidence":[],"label":"pending","reviewer":"","note":"","history":[]} for i,sentence in enumerate(["即时反馈组完成率为80%。","延迟反馈组完成率为65%。","演示没有测量长期效果。"],1)],
            "article":{"title":"反馈综述","kind":"review","sections":[{"title":"发现","paragraphs":[["S001","S002"]]},{"title":"结论","paragraphs":[["S003"]]}]},
            "writing":{"constraints":{"min_characters":20},"character_count":38,"met":True,"issues":[]}}
        self.run["claims"][0].update(label="partial",reviewer="R1",note="保留既有核验",history=[{"label":"partial"}])
        self.operation={"op":"insert_after","anchor":"S003","claim":{"text":"演示结果不能说明长期学习效果。","evidence":[self.evidence]}}

    def response(self,operations=None,message="已在结论末尾加一句，其他内容未变。"):
        return {"message":message,"operations":operations if operations is not None else [self.operation]}

    def model_response(self,operations=None,message="已修改"):
        return {"choices":[{"message":{"content":json.dumps(self.response(operations,message),ensure_ascii=False)}}],"id":"edit-receipt"}

    def test_add_one_keeps_every_existing_sentence_and_annotation_exactly(self):
        before=copy.deepcopy(self.run)
        result,changed=apply_patch(self.run,self.response(),"在结论后加一句话")
        self.assertEqual(self.run,before)
        self.assertEqual(result["claims"][:3],before["claims"])
        self.assertEqual(result["article"]["sections"][0],before["article"]["sections"][0])
        self.assertEqual(result["article"]["sections"][1]["paragraphs"],[["S003","S004"]])
        self.assertEqual(changed["added_ids"],["S004"])
        self.assertTrue(result["claims"][-1]["evidence"][0]["valid"])

    def test_add_one_rejects_whole_rewrite_and_multiple_added_sentences(self):
        cases=[self.response([{**self.operation,"op":"replace","id":"S003"}]),
               self.response([self.operation,self.operation]),
               self.response([{**self.operation,"claim":{**self.operation["claim"],"text":"第一句。第二句。"}}])]
        for output in cases:
            with self.subTest(output=output),self.assertRaises(ValueError):
                apply_patch(self.run,output,"只加一句话")

    def test_only_affirmative_exact_counts_enable_the_exact_guard(self):
        for instruction, count in [
            ("只加一句话", 1), ("仅添加1句", 1), ("在结论后增加两句", 2),
            ("补充3句，不改原文", 3), ("不要改动原文，只加一句", 1),
            ("不要加一句，改成加三句", 3),
            ("加一句，改为加三句", 3),
        ]:
            with self.subTest(instruction=instruction):
                self.assertEqual(addition_count(instruction), count)

    def test_negated_historical_and_approximate_counts_are_not_exact_limits(self):
        for instruction in [
            "不要只加一句。。。。加很多", "别只加1句", "不止加一句",
            "不要仅仅补充一句", "不需要每次都只加一句", "不是让你加一句",
            "不能只加一句，详细展开", "加一句不够，多加一些",
            "上次只加一句，这次详细展开", "把刚加的一句话扩写成一段",
            "我之前说“只加一句”，现在取消这个限制，多写一些",
            "大概加三句", "加两句左右", "补充一两句", "加一句，改为多加一些",
            "多加informer相关内容，详细描述",
        ]:
            with self.subTest(instruction=instruction):
                self.assertEqual(addition_constraints(instruction), {})
                self.assertIsNone(addition_count(instruction))

    def test_sentence_bounds_keep_their_direction(self):
        cases = [
            ("至少加三句", {"min_sentences":3}),
            ("补充至少3句", {"min_sentences":3}),
            ("增加不少于三句", {"min_sentences":3}),
            ("最多增加五句", {"max_sentences":5}),
            ("增加不超过五句", {"max_sentences":5}),
            ("加三句以上", {"min_sentences":3}),
            ("加五句以内", {"max_sentences":5}),
            ("加三到五句", {"min_sentences":3, "max_sentences":5}),
            ("至少加三句，最多加五句", {"min_sentences":3, "max_sentences":5}),
            ("不要只加一句，而是至少加五句", {"min_sentences":5}),
        ]
        for instruction, expected in cases:
            with self.subTest(instruction=instruction):
                self.assertEqual(addition_constraints(instruction), expected)
                self.assertIsNone(addition_count(instruction))

    def test_latest_expansion_overrides_old_single_sentence_request(self):
        self.run["conversation"] = [
            {"role":"user", "content":"只加一句话"},
            {"role":"assistant", "content":"已补充一句。"},
        ]
        operations = [copy.deepcopy(self.operation) for _ in range(3)]
        instruction = "不要只加一句。。。。加很多"
        before = copy.deepcopy(self.run)
        with patch("trace_review.revision.request_model",
                   return_value=self.model_response(operations, "已补充三句。")) as request:
            result = revise_run(self.run, instruction,
                                {"base":"https://api.deepseek.com","model":"mock","key":"test"})
        self.assertEqual(request.call_count, 1)
        self.assertEqual(len(result["last_edit"]["added_ids"]), 3)
        self.assertEqual(result["claims"][:3], before["claims"])
        self.assertEqual(self.run, before)
        payload = json.loads(request.call_args.args[2]["messages"][1]["content"])
        self.assertEqual(payload["addition_constraints"], {})
        self.assertIn("只加一句话", payload["conversation"][0]["content"])

    def test_minimum_and_maximum_validate_ranges_instead_of_exact_counts(self):
        operations = [copy.deepcopy(self.operation) for _ in range(4)]
        for instruction in ["至少加三句", "最多加五句", "加三到五句"]:
            with self.subTest(instruction=instruction):
                result, changed = apply_patch(self.run, self.response(operations), instruction)
                self.assertEqual(len(changed["added_ids"]), 4)
                self.assertEqual(result["claims"][:3], self.run["claims"])
        for instruction in ["至少加五句", "最多加三句"]:
            with self.subTest(instruction=instruction), self.assertRaises(ValueError):
                apply_patch(self.run, self.response(operations), instruction)

    def test_two_insertions_keep_model_order(self):
        second=copy.deepcopy(self.operation)
        second["claim"]["text"]="没有长期观察数据就不能判断长期效果。"
        result,_=apply_patch(self.run,self.response([self.operation,second]),"在最后增加两句")
        self.assertEqual([c["text"] for c in result["claims"][-2:]], [self.operation["claim"]["text"],second["claim"]["text"]])
        self.assertEqual(result["article"]["sections"][-1]["paragraphs"],[["S003","S004","S005"]])

    def test_invalid_quote_and_unknown_anchor_do_not_touch_original(self):
        before=copy.deepcopy(self.run)
        for operation in [{**self.operation,"anchor":"missing"},
                          {**self.operation,"claim":{"text":"新增句子。","evidence":[{**self.evidence,"quote":"编造的引文"}]}}]:
            with self.assertRaises(ValueError):
                apply_patch(self.run,self.response([operation]),"加一句话")
        self.assertEqual(self.run,before)

    def test_replace_keeps_id_and_resets_only_edited_annotation(self):
        op={"op":"replace","id":"S001","claim":self.operation["claim"]}
        result,changed=apply_patch(self.run,self.response([op]),"修改第一句")
        self.assertEqual(result["claims"][0]["id"],"S001")
        self.assertEqual(result["claims"][0]["label"],"pending")
        self.assertEqual(result["claims"][1:],self.run["claims"][1:])
        self.assertEqual(changed["updated_ids"],["S001"])

    def test_delete_cleans_empty_paragraph_without_reindexing(self):
        result,_=apply_patch(self.run,self.response([{"op":"delete","id":"S003"}]),"删掉结论这句")
        self.assertEqual([c["id"] for c in result["claims"]],["S001","S002"])
        self.assertEqual(len(result["article"]["sections"]),1)

    def test_conversation_questions_do_not_change_article(self):
        with patch("trace_review.revision.request_model",return_value=self.model_response([],"这句话表示没有长期观察数据。")):
            result=revise_run(self.run,"最后一句是什么意思",{"base":"https://api.deepseek.com","model":"mock","key":"test"})
        self.assertEqual(result["claims"],self.run["claims"])
        self.assertEqual(result["article"],self.run["article"])
        self.assertNotIn("revisions",result)
        self.assertIn("没有长期",result["conversation"][-1]["content"])

    def test_multiple_turns_send_previous_edit_and_original_requirements(self):
        config={"base":"https://api.deepseek.com","model":"mock","key":"test"}
        with patch("trace_review.revision.request_model",return_value=self.model_response()):
            first=revise_run(self.run,"在结论后加一句话",config)
        payload=edit_payload(first,"把刚加的那句改得简洁些")
        self.assertEqual(payload["initial_requirements"],"不少于20字")
        self.assertEqual(payload["conversation"][-2]["content"],"在结论后加一句话")
        self.assertEqual(payload["current_claims"][-1]["id"],"S004")
        self.assertEqual(payload["last_change"]["added_ids"],["S004"])
        self.assertEqual(payload["recent_edits"][-1]["operations"][0]["anchor"],"S003")
        self.assertEqual(first["revision_number"],1)
        self.assertEqual(first["last_edit"]["receipt"]["request_id"],"edit-receipt")

    def test_wrong_patch_retries_and_original_survives(self):
        bad={"op":"delete","id":"S001"}
        with patch("trace_review.revision.request_model",side_effect=[self.model_response([bad]),self.model_response()]) as request:
            result=revise_run(self.run,"加一句话",{"base":"https://api.deepseek.com","model":"mock","key":"test"})
        self.assertEqual(request.call_count,2)
        self.assertEqual(result["last_edit"]["attempts"],2)
        self.assertEqual(result["claims"][:3],self.run["claims"])

    def test_followup_searches_uncited_pages_and_nearby_context(self):
        doc = self.run["corpus"]["documents"][0]
        for number, text in [(2, "We implement the forecasting module based on Informer [47]."),
                             (3, "The encoder employs ProbSparse self-attention and distilling.")]:
            doc["pages"].append({"number":number, "text":text, "text_sha256":digest(text)})
        self.run["claims"][0]["evidence"] = [self.evidence]
        self.run["conversation"] = [{"role":"assistant", "content":"文献没有出现Informer。"},
                                    {"role":"user", "content":"把Informer相关也加进去"}]
        payload = edit_payload(self.run, "把论文里出现的核心写进去")
        self.assertTrue(any(e["page"] == 2 and "Informer" in e["quote"] for e in payload["evidence"]))
        self.assertTrue(any(e["page"] == 3 and "ProbSparse" in e["quote"] for e in payload["evidence"]))
        self.assertEqual(payload["source_search"][0]["pages"], [2])

    def test_evidence_id_resolves_to_exact_source_and_survives_undo(self):
        payload = edit_payload(self.run, "加一句话")
        passage = next(e for e in payload["evidence"] if self.evidence["quote"] in e["quote"])
        op = copy.deepcopy(self.operation)
        op["claim"]["evidence"] = [{"evidence_id":passage["evidence_id"]}]
        with patch("trace_review.revision.request_model", return_value=self.model_response([op])) as request:
            edited = revise_run(self.run, "加一句话", {"base":"https://api.deepseek.com","model":"mock","key":"test"})
        evidence = edited["claims"][-1]["evidence"][0]
        page = self.run["corpus"]["documents"][0]["pages"][0]["text"]
        self.assertEqual(evidence["quote"], page[evidence["start"]:evidence["end"]])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(edited["claims"][:3], self.run["claims"])
        self.assertEqual(edited["revisions"][-1]["operations"][0]["claim"]["evidence"][0]["quote"], passage["quote"])
        self.assertEqual(undo_revision(edited)["claims"], self.run["claims"])

    def test_bad_evidence_gets_targeted_retry_context(self):
        op = copy.deepcopy(self.operation)
        op["claim"]["evidence"] = [{"evidence_id":"invented"}]
        with patch("trace_review.revision.request_model",
                   side_effect=[self.model_response([op]), self.model_response()]) as request:
            revised = revise_run(self.run, "加一句话", {"base":"https://api.deepseek.com","model":"mock","key":"test"})
        feedback = json.loads(request.call_args_list[1].args[2]["messages"][1]["content"])["correction"]
        self.assertEqual(feedback["invalid_evidence"][0]["operation_index"], 0)
        self.assertEqual(feedback["invalid_evidence"][0]["evidence_id"], "invented")
        self.assertEqual(feedback["previous_patch"]["operations"], [op])
        self.assertEqual(revised["last_edit"]["attempts"], 2)

    def test_legacy_wrapped_quote_is_repaired_without_another_model_call(self):
        text = "The encoder uses self-\nattention and hidden represen-\ntations."
        page = self.run["corpus"]["documents"][0]["pages"][0]
        page.update(text=text, text_sha256=digest(text))
        op = copy.deepcopy(self.operation)
        op["claim"]["evidence"] = [{**self.evidence,"quote":"The encoder uses self-attention and hidden representations."}]
        with patch("trace_review.revision.request_model", return_value=self.model_response([op])) as request:
            revised = revise_run(self.run, "加一句话", {"base":"https://api.deepseek.com","model":"mock","key":"test"})
        self.assertEqual(request.call_count, 1)
        self.assertEqual(revised["claims"][-1]["evidence"][0]["quote"], text)

    def test_edit_reply_without_patch_is_rejected(self):
        for instruction in ["把刚加的那句改短", "你就把informer这些论文里出现了的核心写进去"]:
            with self.subTest(instruction=instruction), self.assertRaisesRegex(ValueError,"实际修改"):
                apply_patch(self.run,self.response([],"已修改"),instruction)

    def test_undo_keeps_new_annotations_on_untouched_sentences(self):
        with patch("trace_review.revision.request_model",return_value=self.model_response()):
            edited=revise_run(self.run,"加一句话",{"base":"https://api.deepseek.com","model":"mock","key":"test"})
        edited["claims"][0]["note"]="新增人工核验说明"
        restored=undo_revision(edited)
        self.assertEqual(restored["claims"][0]["note"],"新增人工核验说明")
        self.assertEqual(restored["claims"][0]["text"],self.run["claims"][0]["text"])

    def test_progress_reports_observable_stages_not_model_reasoning(self):
        response=self.model_response()
        response["choices"][0]["message"]["reasoning_content"]="private-internal-reasoning"
        events=[]
        with patch("trace_review.revision.request_model",return_value=response):
            result=revise_run(self.run,"加一句话",{"base":"https://api.deepseek.com","model":"mock","key":"test"},
                              progress=lambda stage,message:events.append((stage,message)))
        self.assertEqual([event[0] for event in events],["context","reply","validation"])
        self.assertNotIn("private-internal-reasoning",json.dumps({"run":result,"events":events}))

    def test_undo_restores_exact_original_article_and_claims(self):
        with patch("trace_review.revision.request_model",return_value=self.model_response()):
            edited=revise_run(self.run,"加一句话",{"base":"https://api.deepseek.com","model":"mock","key":"test"})
        restored=undo_revision(edited)
        self.assertEqual(restored["article"],self.run["article"])
        self.assertEqual(restored["claims"],self.run["claims"])
        self.assertEqual(restored["revisions"],[])
        self.assertEqual(restored["revision_number"],2)
        self.assertIn("撤销",restored["conversation"][-1]["content"])


if __name__=="__main__":
    unittest.main()
