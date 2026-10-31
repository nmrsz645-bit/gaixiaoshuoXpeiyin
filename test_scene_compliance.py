import copy
import hashlib
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from novel_monitor.scene_document import (
    SceneProtocolError, parse_document, parse_edit_ops, compile_edits,
)
from novel_monitor.scene_model import SceneModelClient
from novel_monitor.scene_review import parse_core_facts, extract_core_facts, review_scenes_and_rewrite
from novel_monitor.scene_review import SceneReviewResult


def raw_op(kind, ids, replacement=(), facts=()):
    return dict(kind=kind, source_ids=list(ids), replacement=list(replacement),
                reason="去除非必要细节", preserved_fact_ids=list(facts))


def with_no_added_facts(provider):
    """Default secondary-audit responses for fixtures testing other stages."""
    def request(**kwargs):
        payload=kwargs.get('payload',{})
        if payload.get('task')=='audit_edit_pairs':
            return dict(verdicts=[dict(pair_id=p['id'],verdict='ok',reason='本测试聚焦其他核对阶段',
                source_quote='',output_quote='') for p in payload['pairs']])
        if payload.get('task')=='audit_additions':
            return dict(additions=[],checked_output_count=payload['output_count'],
                        checked_candidate_sha256=payload['candidate_sha256'])
        return provider(**kwargs)
    return request


class DocumentTests(unittest.TestCase):
    def test_scene_delta_retry_explicitly_forbids_extra_bridge_and_keep_records(self):
        from novel_monitor.scene_review import _parse_scene_edits
        doc=parse_document('原文。');ids=[doc.units[0].id]
        payload=dict(scope_id='x',required_target_count=1)
        def request(**kw):
            if '纠正请求：仅允许rewrite/delete/compress' in kw['prompt']:
                return dict(operations=[raw_op('rewrite',ids,['修改。'])],checked_source_count=1,checked_scope='x')
            return dict(operations=[raw_op('bridge',[],['整稿。'])],checked_source_count=1,checked_scope='x')
        with tempfile.TemporaryDirectory() as tmp,patch('novel_monitor.scene_model._request_json',side_effect=request):
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1,checkpoint_dir=Path(tmp))
            ops=c.ask('edit_scene','base',payload,lambda v:_parse_scene_edits(v,doc,ids,(),(),'x'))
        self.assertEqual(compile_edits(doc,ops).text,'修改。')
    def test_delta_edits_preserve_previous_current_changes_and_untouched_text(self):
        from novel_monitor.scene_review import _parse_scene_edits
        doc=parse_document('甲。\n乙。\n结局。')
        ids=[u.id for u in doc.units]
        prior=parse_edit_ops({'operations':[raw_op('rewrite',[ids[0]],['已通过的修改。'])]},doc,[ids[0]],())
        response=dict(operations=[raw_op('rewrite',[ids[1]],['局部修改。'])],checked_source_count=3,checked_scope='scope')
        ops=_parse_scene_edits(response,doc,ids,(),prior,'scope')
        self.assertEqual(compile_edits(doc,ops).text,'已通过的修改。\n局部修改。\n结局。')
        self.assertEqual(_parse_scene_edits(dict(response,operations=[]),doc,ids,(),prior,'scope'),prior)
        for bad in [dict(response,checked_source_count=2),dict(response,checked_scope='other'),
                    dict(response,checked_source_count=True),{'operations':response['operations']},
                    dict(response,operations=[raw_op('delete',['unknown'])]),
                    dict(response,operations=response['operations']+[raw_op('keep',[ids[1]])])]:
            with self.subTest(bad=bad),self.assertRaises(SceneProtocolError):
                _parse_scene_edits(bad,doc,ids,(),prior,'scope')

    def test_delta_cannot_split_an_existing_compressed_atom(self):
        from novel_monitor.scene_review import _parse_scene_edits
        doc=parse_document('甲。\n乙。\n结局。')
        ids=[u.id for u in doc.units]
        prior=parse_edit_ops({'operations':[raw_op('compress',ids[:2],['已有压缩。'])]},doc,ids[:2],())
        response=dict(operations=[raw_op('rewrite',[ids[0]],['片段。'])],checked_source_count=3,checked_scope='scope')
        with self.assertRaises(SceneProtocolError):_parse_scene_edits(response,doc,ids,(),prior,'scope')
        response['operations']=[raw_op('keep',ids[:2])]
        ops=_parse_scene_edits(response,doc,ids,(),prior,'scope')
        self.assertEqual(compile_edits(doc,ops).text,doc.original)

    def test_delta_keep_missing_empty_replacement_is_unambiguous(self):
        from novel_monitor.scene_review import _parse_scene_edits
        doc=parse_document('原文。')
        op=raw_op('keep',[doc.units[0].id]);del op['replacement']
        ops=_parse_scene_edits(dict(operations=[op],checked_source_count=1,checked_scope='x'),doc,[doc.units[0].id],(),(),'x')
        self.assertEqual(compile_edits(doc,ops).text,doc.original)

    def test_disjoint_rewrite_requires_exact_one_paragraph_per_source(self):
        from novel_monitor.scene_review import _parse_scene_edits
        doc=parse_document('甲。\n保留中段。\n丙。')
        ids=[u.id for u in doc.units]
        response=dict(operations=[raw_op('rewrite',[ids[0],ids[2]],['改甲。','改丙。'])],checked_source_count=3,checked_scope='x')
        ops=_parse_scene_edits(response,doc,ids,(),(),'x')
        self.assertEqual(compile_edits(doc,ops).text,'改甲。\n保留中段。\n改丙。')
        for replacement in [['仅一段。'],['甲。\n混合段。','丙。'],['甲。','丙。','多余段。']]:
            response['operations'][0]['replacement']=replacement
            with self.subTest(replacement=replacement),self.assertRaises(SceneProtocolError):
                _parse_scene_edits(response,doc,ids,(),(),'x')

    def test_delta_correction_identifies_missing_fields_and_empty_bridge(self):
        from novel_monitor.scene_review import _parse_scene_edits
        from novel_monitor.scene_document import exact_keys
        with self.assertRaisesRegex(SceneProtocolError,'reason'):
            exact_keys({'kind':'keep'},{'kind','reason'})
        doc=parse_document('原文。');ids=[doc.units[0].id]
        value=dict(operations=[raw_op('bridge',[],['整场成稿。'])],checked_source_count=1,checked_scope='x')
        with self.assertRaisesRegex(SceneProtocolError,'bridge.*source_ids'):
            _parse_scene_edits(value,doc,ids,(),(),'x')

    def test_multiline_replacement_is_losslessly_split_into_paragraphs(self):
        doc=parse_document("原文一。\n原文二。")
        ids=[u.id for u in doc.units]
        ops=parse_edit_ops({"operations":[raw_op("compress",ids,["新段一。\r\n\r\n新段二。"]) ]},doc,ids,())
        self.assertEqual(compile_edits(doc,ops).text,"新段一。\n\n新段二。")
        ops=parse_edit_ops({"operations":[raw_op("compress",ids,["新段一。","","新段二。"]) ]},doc,ids,())
        self.assertEqual(compile_edits(doc,ops).text,"新段一。\n\n新段二。")

    def test_redundant_keep_records_never_override_one_unambiguous_edit(self):
        doc=parse_document("原文甲。\n风险细节。\n结局。")
        ids=[u.id for u in doc.units]
        raw={"operations":[raw_op("compress",ids,["原文甲。", "结局。"]),raw_op("keep",ids)]}
        ops=parse_edit_ops(raw,doc,ids,())
        result=compile_edits(doc,ops)
        self.assertEqual(result.text,"原文甲。\n结局。")
        self.assertEqual(len(result.source_mapping),3)
        self.assertEqual(len(result.edit_actions),1)

    def test_duplicate_edit_feedback_allows_model_to_remove_overlapping_container(self):
        doc=parse_document("前文。\n风险细节。\n结局。")
        ids=[u.id for u in doc.units]
        good={"operations":[raw_op("compress",ids,["前文。", "结局。"]) ]}
        bad={"operations":good["operations"]+[raw_op("rewrite",[ids[0]],["冲突的另一份改稿。"])]}
        def request(**kw):
            return good if kw["payload"].get("protocol_hints",{}).get("duplicate_ids") else bad
        with tempfile.TemporaryDirectory() as tmp,patch("novel_monitor.scene_model._request_json",side_effect=with_no_added_facts(request)):
            c=SceneModelClient(api_key="test",model="test",url="https://test.invalid",timeout=1,checkpoint_dir=Path(tmp))
            ops=c.ask("edit_scene","prompt",{},lambda v:parse_edit_ops(v,doc,ids,()))
        self.assertEqual(compile_edits(doc,ops).text,"前文。\n结局。")

    def test_unordered_operations_and_disjoint_keeps_compile_in_original_order(self):
        doc=parse_document("甲。\n乙。\n丙。\n丁。\n戊。")
        ids=[u.id for u in doc.units]
        raw={"operations":[raw_op("rewrite",[ids[2]],["修改丙。"]),raw_op("keep",[ids[4],ids[0],ids[1],ids[3]])]}
        ops=parse_edit_ops(raw,doc,ids,())
        self.assertEqual(compile_edits(doc,ops).text,"甲。\n乙。\n修改丙。\n丁。\n戊。")

    def test_multi_source_keep_has_exact_per_unit_offsets_and_lines(self):
        doc=parse_document("甲。\n乙。\n结局。")
        ops=parse_edit_ops({"operations":[raw_op("keep",[u.id for u in doc.units])]},doc,[u.id for u in doc.units],())
        result=compile_edits(doc,ops)
        self.assertEqual([(m["output_start"],m["output_end"],m["output_lines"]) for m in result.source_mapping],
                         [(0,3,[1]),(3,6,[2]),(6,9,[3])])

    def test_operations_cannot_silently_reorder_or_overlap_compiled_ranges(self):
        from novel_monitor.scene_document import EditOp
        doc=parse_document("甲。\n乙。\n丙。")
        with self.assertRaises(SceneProtocolError):
            compile_edits(doc,(EditOp("delete",("s000001","s000002"),(),"删除",()),
                               EditOp("rewrite",("s000002",),("重复覆盖。",),"改写",())))

    def test_keep_reconstructs_crlf_blank_lines_and_long_units(self):
        text = "开篇。\r\n\r\n" + "长段。" * 15 + "\r\n结局。"
        doc = parse_document(text, max_unit_chars=12)
        self.assertEqual(compile_edits(doc, ()).text, text)
        self.assertEqual("".join(u.text for u in doc.units), text)
        self.assertTrue(all(len(u.text) <= 12 for u in doc.units))
        self.assertEqual(doc.units[-1].source_line, 4)

    def test_duplicate_sentence_changes_only_targeted_id(self):
        doc = parse_document("重复。\n中间。\n重复。", max_unit_chars=100)
        ops = parse_edit_ops({"operations": [raw_op("rewrite", [doc.units[2].id], ["末段。"])]},
                             doc, [doc.units[2].id], ())
        self.assertEqual(compile_edits(doc, ops).text, "重复。\n中间。\n末段。")

    def test_deletion_over_twenty_percent_retains_ending_and_mapping(self):
        doc = parse_document("起因。\n" + "细节。" * 40 + "\n结局承担债务。", 500)
        ops = parse_edit_ops({"operations": [raw_op("delete", [doc.units[1].id])]},
                             doc, [doc.units[1].id], ())
        result = compile_edits(doc, ops)
        self.assertEqual(result.text, "起因。\n结局承担债务。")
        self.assertEqual(result.source_mapping[1]["output_lines"], [])
        self.assertEqual(result.source_mapping[2]["output_lines"], [2])
        self.assertEqual(result.edit_actions[0]["original"], "细节。" * 40 + "\n")

    def test_missing_duplicate_unknown_out_of_order_and_empty_rewrite_rejected(self):
        doc = parse_document("甲。\n乙。\n丙。", 100)
        ids = [u.id for u in doc.units]
        cases = [[], [raw_op("keep", [ids[0]])] * 2,
                 [raw_op("keep", ["missing"])],
                 [raw_op("compress", ids[::-1], ["合并。"])],
                 [raw_op("rewrite", ids, [])],
                 [raw_op("delete", ids, ["偷加文字"])],
                 [dict(raw_op("delete", ids), surprise=True)]]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(SceneProtocolError):
                parse_edit_ops({"operations": value}, doc, ids, ())

    def test_compression_maps_every_source_to_actual_output_lines(self):
        doc = parse_document("前言。\n刺激细节一。\n刺激细节二。\n结局。", 100)
        ids = [u.id for u in doc.units[1:3]]
        ops = parse_edit_ops({"operations": [raw_op("compress", ids, ["他受伤后离开。", "事情到此结束。"])]}, doc, ids, ())
        result = compile_edits(doc, ops)
        self.assertEqual(result.text, "前言。\n他受伤后离开。\n事情到此结束。\n结局。")
        self.assertEqual(result.source_mapping[1]["output_lines"], [2, 3])
        self.assertEqual(result.source_mapping[2]["output_lines"], [2, 3])


def fact(fid, kind, unit, evidence, statement=None):
    return dict(id=fid, kind=kind, statement=statement or evidence,
                source_ids=[unit.id], evidence=evidence)


class CoreFactTests(unittest.TestCase):
    def test_recorded_api_failure_can_be_revalidated_without_repeating_the_request(self):
        def old_parser(raw):
            raise SceneProtocolError('旧协议不接受数字来源编号')
        def current_parser(raw):
            if type(raw.get('source_ref')) is not int or raw['source_ref'] not in {1,2}:
                raise SceneProtocolError('未知来源编号')
            return raw['source_ref']
        with tempfile.TemporaryDirectory() as tmp:
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1,checkpoint_dir=Path(tmp))
            with patch('novel_monitor.scene_model._request_json',return_value=dict(source_ref=1)):
                with self.assertRaises(SceneProtocolError):c.ask('extract_core_global','same prompt',dict(book='A'),old_parser)
            with patch('novel_monitor.scene_model._request_json',side_effect=AssertionError('不应重复API')):
                self.assertEqual(c.ask('extract_core_global','same prompt',dict(book='A'),current_parser),1)
            with patch('novel_monitor.scene_model._request_json',return_value=dict(source_ref=2)) as request:
                self.assertEqual(c.ask('extract_core_global','same prompt',dict(book='B'),current_parser),2)
                self.assertEqual(request.call_count,1)

    def test_many_known_references_are_not_mistaken_for_missing_fact_evidence(self):
        from novel_monitor.scene_review import _global_core_facts
        doc=parse_document('\n'.join(['经历。']*20+['结局。']))
        facts=_global_core_facts(dict(facts=[dict(id='a',kind='event',statement='多次经历',
            source_ids=list(range(1,21)),evidence_source_id=1)]),doc)
        self.assertEqual(len(facts[0].source_ids),20)

    def test_global_source_reference_quotes_original_and_adds_a_deterministic_tail_guard(self):
        from novel_monitor.scene_review import _global_core_facts
        doc=parse_document('她受到伤害。\n她决定离开。\n最终离婚。')
        raw=dict(facts=[dict(id='a',kind='event',statement='受到伤害',source_ids=['s0001'],evidence_source_id=1)])
        facts=_global_core_facts(raw,doc)
        self.assertEqual(facts[0].source_ids,('s000001',))
        self.assertEqual(facts[0].evidence,'她受到伤害。')
        self.assertEqual(facts[-1].kind,'ending')
        self.assertEqual(facts[-1].evidence,'最终离婚。')
        for wrong in [999,2]:
            with self.assertRaises(SceneProtocolError):
                _global_core_facts(dict(facts=[dict(raw['facts'][0],evidence_source_id=wrong)]),doc)

    def test_whole_book_mainline_uses_compact_source_and_independent_full_coverage(self):
        lines=['她遭到拘禁。']+['无关背景。']*40+['她决定离开。']+['旅途。']*40+['最终离婚。']
        doc=parse_document('\n'.join(lines));seen=[]
        initial=[dict(id='a',kind='event',statement='遭到拘禁',source_ids=[1],evidence=lines[0]),
                 dict(id='b',kind='ending',statement='最终离婚',source_ids=[str(len(lines))],evidence=lines[-1])]
        def request(**kw):
            p=kw['payload'];seen.append(p)
            self.assertIn('42|她决定离开。',p['source_text'])
            if p['task']=='extract_core_global':
                return dict(facts=initial)
            middle=dict(id='c',kind='event',statement='决定离开',source_ids=['42'],evidence=lines[41])
            return dict(facts=[initial[0],middle,initial[1]],checked_source_range=p['source_range'],
                checked_source_count=p['source_count'],checked_document_sha256=p['document_sha256'])
        with tempfile.TemporaryDirectory() as tmp,patch('novel_monitor.scene_model._request_json',side_effect=with_no_added_facts(request)):
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1,checkpoint_dir=Path(tmp));c.max_chars=8000
            facts=extract_core_facts(doc,c,8000)
        self.assertEqual(len(seen),2)
        self.assertEqual([f.source_ids for f in facts],[('s000001',),('s000042',),('s000083',)])
        self.assertTrue(all(len(json.dumps(p,ensure_ascii=False))<=8000 for p in seen))

    def test_whole_book_verification_rejects_wrong_coverage_and_unknown_numeric_source(self):
        doc=parse_document('\n'.join(['开头。']+['背景。']*80+['结局。']))
        valid=[dict(id='f1',kind='ending',statement='结局',source_ids=[len(doc.units)],evidence='结局。')]
        for bad_ids in (False,True):
            def request(**kw):
                p=kw['payload'];facts=valid if not bad_ids else [dict(valid[0],source_ids=[9999])]
                if p['task']=='extract_core_global':return dict(facts=facts)
                return dict(facts=valid,checked_source_range=p['source_range'],checked_source_count=p['source_count']-1,
                    checked_document_sha256=p['document_sha256'])
            with tempfile.TemporaryDirectory() as tmp,patch('novel_monitor.scene_model._request_json',side_effect=with_no_added_facts(request)):
                c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1,checkpoint_dir=Path(tmp));c.max_chars=8000
                with self.assertRaises(SceneProtocolError):
                    extract_core_facts(doc,c,8000)

    @unittest.skipUnless(os.name=="nt","Windows path budget")
    def test_nested_windows_checkpoint_can_save_failed_responses(self):
        def reject(value):
            raise SceneProtocolError("bad schema")
        with tempfile.TemporaryDirectory() as tmp,patch("novel_monitor.scene_model._request_json",return_value={}):
            directory=Path(tmp)/("a"*60)/("b"*45)
            c=SceneModelClient(api_key="test",model="test",url="https://test.invalid",timeout=1,checkpoint_dir=directory)
            with self.assertRaises(SceneProtocolError):
                c.ask("edit_scene","prompt",{},reject)
            self.assertEqual(len(list(directory.glob('*.invalid-*.json'))),3)

    def test_repeated_protocol_failures_preserve_each_rejected_response(self):
        def reject(value):
            raise SceneProtocolError("invalid response")
        with tempfile.TemporaryDirectory() as tmp,patch("novel_monitor.scene_model._request_json",return_value={}):
            c=SceneModelClient(api_key="test",model="test",url="https://test.invalid",timeout=1,checkpoint_dir=Path(tmp))
            for _ in range(2):
                with self.assertRaises(SceneProtocolError):
                    c.ask("extract_core","prompt",{},reject)
            self.assertEqual(len(list(Path(tmp).glob('*.invalid-*.json'))),6)

    def test_quote_layout_is_canonicalized_without_accepting_word_changes(self):
        doc=parse_document("她离开。\r\n　　最终离婚。")
        f=dict(id="f1",kind="ending",statement="分开离婚",source_ids=["s000001","s000002"],evidence="她离开。最终离婚。")
        facts=parse_core_facts({"facts":[f]},doc,True)
        self.assertEqual(facts[0].evidence,"她离开。\r\n　　最终离婚。")
        with self.assertRaises(SceneProtocolError):
            parse_core_facts({"facts":[dict(f,evidence="她离开。最终病逝。")]},doc,True)

    def test_correction_supplies_exact_original_quote_but_still_requires_valid_response(self):
        doc=parse_document("前文。\n最终离婚。")
        bad={"facts":[fact("f1","ending",doc.units[-1],"最终病逝")]}
        good={"facts":[fact("f1","ending",doc.units[-1],"最终离婚")]}
        def request(**kw):
            hints=kw["payload"].get("protocol_hints",[])
            return good if "最终离婚。" in json.dumps(hints,ensure_ascii=False) else bad
        with tempfile.TemporaryDirectory() as tmp,patch("novel_monitor.scene_model._request_json",side_effect=with_no_added_facts(request)):
            client=SceneModelClient(api_key="test",model="test",url="https://test.invalid",timeout=1,checkpoint_dir=Path(tmp))
            facts=client.ask("extract_core","prompt",{},lambda v:parse_core_facts(v,doc,True))
        self.assertEqual(facts[0].evidence,"最终离婚")

    def test_ending_can_span_multiple_units_but_must_include_real_tail(self):
        doc=parse_document("前文。\n他承担债务。\n她离开。\n\n")
        item=dict(id="f1",kind="ending",statement="承担债务后分开",source_ids=["s000002","s000003"],evidence="他承担债务。\n她离开。")
        self.assertEqual(parse_core_facts({"facts":[item]},doc,True)[0].kind,"ending")

    def test_invalid_model_evidence_is_corrected_without_accepting_hallucinated_quote(self):
        doc=parse_document("她离开。\n最终离婚。")
        bad={"facts":[fact("f1","ending",doc.units[-1],"最终病逝") ]}
        good={"facts":[fact("f1","ending",doc.units[-1],"最终离婚") ]}
        with tempfile.TemporaryDirectory() as tmp, patch("novel_monitor.scene_model._request_json",side_effect=[bad,good]) as req:
            client=SceneModelClient(api_key="test",model="test",url="https://test.invalid",timeout=1,checkpoint_dir=Path(tmp))
            facts=client.ask("extract_core","prompt",{"source":[u.text for u in doc.units]},lambda v:parse_core_facts(v,doc,True))
            self.assertEqual(facts[0].evidence,"最终离婚")
            self.assertEqual(req.call_count,2)

    def test_same_paragraph_ids_in_different_books_do_not_share_checkpoint(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as tmp, patch("novel_monitor.scene_model._request_json", side_effect=lambda **kw: {"book":kw["payload"]["sha256"]}):
            def ask(text):
                doc = parse_document(text)
                client = SceneModelClient(api_key="test", model="test", url="https://test.invalid", timeout=1, checkpoint_dir=Path(tmp))
                return client.ask("extract_core", "prompt", {"sha256":doc.sha256,"ids":["s000001"]}, lambda x:x["book"])
            with ThreadPoolExecutor(2) as executor:
                actual = list(executor.map(ask, ["甲书。", "乙书。", "甲书。", "乙书。"] ))
            self.assertNotEqual(actual[0], actual[1])
            self.assertEqual(actual[:2], actual[2:])

    def test_ending_requires_tail_evidence_and_real_source_ids(self):
        doc = parse_document("争执。\n后来受伤。\n结局离婚。", 100)
        valid = fact("f1", "ending", doc.units[-1], "结局离婚")
        for item in [dict(valid, evidence="编造的结局"),
                     dict(valid, source_ids=["unknown"]),
                     fact("f1", "ending", doc.units[0], "争执")]:
            with self.subTest(item=item), self.assertRaises(SceneProtocolError):
                parse_core_facts({"facts": [item]}, doc, True)
        with self.assertRaisesRegex(SceneProtocolError, "结局"):
            parse_core_facts({"facts": [fact("f1", "event", doc.units[0], "争执")]}, doc, True)
        self.assertEqual(parse_core_facts({"facts": [valid]}, doc, True)[0].kind, "ending")

    def test_verified_ledger_can_add_missed_core_event(self):
        doc = parse_document("发生拘禁。\n最终断绝关系。", 100)
        ending = fact("f1", "ending", doc.units[1], "最终断绝关系")
        event = fact("f2", "event", doc.units[0], "发生拘禁")
        def request(**kwargs):
            if kwargs["payload"]["task"] == "extract_core":
                return {"facts": [ending]}
            return {"facts": [event, ending], "checked_source_ids": [u.id for u in doc.units]}
        with tempfile.TemporaryDirectory() as tmp, patch("novel_monitor.scene_model._request_json", side_effect=with_no_added_facts(request)):
            client = SceneModelClient(api_key="test", model="test", url="https://test.invalid", timeout=1, checkpoint_dir=Path(tmp))
            facts = extract_core_facts(doc, client, 1000)
        self.assertEqual({f.statement for f in facts}, {"发生拘禁", "最终断绝关系"})

    def test_cached_response_is_validated_and_prompt_is_in_cache_key(self):
        with tempfile.TemporaryDirectory() as tmp, patch("novel_monitor.scene_model._request_json", return_value={"ok": True}) as req:
            client = SceneModelClient(api_key="test", model="test", url="https://test.invalid", timeout=1, checkpoint_dir=Path(tmp))
            client.ask("extract_core", "A", {}, lambda x: x["ok"])
            client.ask("extract_core", "A", {}, lambda x: x["ok"])
            client.ask("extract_core", "B", {}, lambda x: x["ok"])
            self.assertEqual(req.call_count, 2)
            with self.assertRaises(KeyError):
                client.ask("extract_core", "A", {}, lambda x: x["missing"])

    def test_duplicate_fact_ids_and_incomplete_checked_coverage_rejected(self):
        doc = parse_document("最终离开。", 100)
        f = fact("f1", "ending", doc.units[0], "最终离开")
        with self.assertRaises(SceneProtocolError):
            parse_core_facts({"facts": [f, f]}, doc, True)
        with tempfile.TemporaryDirectory() as tmp, patch("novel_monitor.scene_model._request_json", side_effect=lambda **kw: {"facts": [f]} if kw["payload"]["task"]=="extract_core" else {"facts": [f], "checked_source_ids": []}):
            client = SceneModelClient(api_key="test", model="test", url="https://test.invalid", timeout=1, checkpoint_dir=Path(tmp))
            with self.assertRaises(SceneProtocolError):
                extract_core_facts(doc, client, 1000)


class SceneReviewTests(unittest.TestCase):
    def test_unchanged_wording_does_not_waive_a_new_distant_time_contradiction(self):
        from novel_monitor.scene_document import EditOp
        from novel_monitor.scene_review import _verify_blocking
        doc=parse_document('她是昨晚离开的。\n过渡。\n过渡二。\n她想起过去。\n她离开的时间是昨夜。\n结局。')
        rendered=compile_edits(doc,(EditOp('rewrite',('s000001',),('她是今早离开的。',),'改错时间',()),))
        problem=dict(kind='coherence',source_ids=['s000005'],output_ids=['o000005'],
            evidence='她离开的时间是昨夜。',related_quote='',reason='昨夜时间词用词错误，与前文今早自相矛盾')
        response=dict(verdicts=[dict(finding_id='p1',verdict='confirmed',reason='前文被改为今早，后文仍是昨夜，新增时间矛盾',
            source_quote='她离开的时间是昨夜。',output_quote='她离开的时间是昨夜。')])
        with patch('novel_monitor.scene_model._request_json',return_value=response):
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1)
            result=_verify_blocking(doc,rendered,(),[problem],c)
        self.assertEqual(result[0]['kind'],'coherence')

    def test_pair_screen_pages_preserve_every_complete_proposal_within_budget(self):
        from novel_monitor.scene_document import EditOp
        from novel_monitor.scene_review import _screen_edit_proposals
        doc=parse_document('\n'.join(f'第{i}段。'+('原细节。'*30) for i in range(15)))
        ops=tuple(EditOp('rewrite',(u.id,),('简洁描述。'*6,),'概括',()) for u in doc.units)
        seen=[];calls=[]
        def provider(**kw):
            p=kw['payload'];calls.append(p)
            self.assertLessEqual(len(json.dumps(p,ensure_ascii=False)),2000)
            for pair in p['pairs']:
                self.assertEqual(pair['source'],doc.units[int(pair['source_ids'][0][1:])-1].text)
                seen.append(pair['id'])
            return dict(verdicts=[dict(pair_id=v['id'],verdict='ok',reason='同事件概括',source_quote='',output_quote='') for v in p['pairs']])
        with patch('novel_monitor.scene_model._request_json',side_effect=provider):
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1);c.max_chars=2000
            accepted,prior,actions=_screen_edit_proposals(doc,ops,(),c,2000)
        self.assertEqual(len(seen),15);self.assertEqual(len(set(seen)),15)
        self.assertGreater(len(calls),1);self.assertEqual(accepted,ops);self.assertEqual(actions,())

    def test_pair_screen_rejects_event_substitution_and_new_risk_but_preserves_other_edits(self):
        from novel_monitor.scene_document import EditOp
        from novel_monitor.scene_review import _screen_edit_proposals
        cases=[('他以侵害相威胁。','他以关押相威胁。','event_type_changed'),
               ('她跳入湖中。','她以死明志，跃入湖中。','risk_increased')]
        for before,after,verdict in cases:
            doc=parse_document(before+'\n手指疼痛发紫。\n结局离开。')
            bad=EditOp('rewrite',('s000001',),(after,),'错误建议',())
            safe=EditOp('rewrite',('s000002',),('手指受伤。',),'删感官细节',())
            def provider(**kw):
                p=kw['payload']
                return dict(verdicts=[dict(pair_id=v['id'],verdict=verdict if v['source_ids']==['s000001'] else 'ok',
                    reason='不得改换事件或新增风险',source_quote=v['source'].strip() if v['source_ids']==['s000001'] else '',
                    output_quote=v['proposal'].strip() if v['source_ids']==['s000001'] else '') for v in p['pairs']])
            with self.subTest(verdict=verdict),patch('novel_monitor.scene_model._request_json',side_effect=provider):
                c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1)
                ops,previous,actions=_screen_edit_proposals(doc,(bad,safe),(bad,),c,8000)
            self.assertEqual(compile_edits(doc,ops).text,before+'\n手指受伤。\n结局离开。')
            self.assertEqual(previous,())
            self.assertEqual(actions[0]['outcome'],'proposal_rejected')
            self.assertEqual(actions[0]['verdict'],verdict)

    def test_pair_screen_requires_complete_known_verdicts_and_literal_rejection_evidence(self):
        from novel_monitor.scene_document import EditOp
        from novel_monitor.scene_review import _screen_edit_proposals
        doc=parse_document('他以侵害相威胁。')
        op=EditOp('rewrite',('s000001',),('他以关押相威胁。',),'错误建议',())
        for response in [dict(verdicts=[]),dict(verdicts=[dict(pair_id='unknown',verdict='ok',reason='安全',source_quote='',output_quote='')]),
                         dict(verdicts=[dict(pair_id='e000001',verdict='event_type_changed',reason='事件变质',source_quote='不存在的原句',output_quote='他以关押相威胁。')])]:
            with self.subTest(response=response),patch('novel_monitor.scene_model._request_json',return_value=response):
                c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1)
                with self.assertRaises(SceneProtocolError):_screen_edit_proposals(doc,(op,),(),c,8000)

    def test_rejected_later_proposal_restores_earlier_accepted_edit(self):
        from novel_monitor.scene_document import EditOp
        from novel_monitor.scene_review import _screen_edit_proposals
        doc=parse_document('他辱骂我。\n结局。')
        prior=EditOp('rewrite',('s000001',),('他言语冒犯。',),'已接受',())
        bad=EditOp('rewrite',('s000001',),('他狠狠打了我。',),'新错误',())
        response=dict(verdicts=[dict(pair_id='e000001',verdict='event_type_changed',reason='辱骂不能改成殴打',
            source_quote='他辱骂我。',output_quote='他狠狠打了我。')])
        with patch('novel_monitor.scene_model._request_json',return_value=response):
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1)
            accepted,previous,actions=_screen_edit_proposals(doc,(bad,),(prior,),c,8000)
        self.assertEqual(accepted,(prior,));self.assertEqual(previous,(prior,))
        self.assertEqual(compile_edits(doc,accepted).text,'他言语冒犯。\n结局。')

    def test_only_proven_unchanged_local_source_typo_can_override_a_bad_confirmation(self):
        from novel_monitor.scene_document import EditOp
        from novel_monitor.scene_review import _verify_blocking
        original='辱骂。\n过渡。\n仍是过渡。\n她醒来。\n医院的顾灯。\n结局。'
        def verify(text,extra=(),related='',reason='顾灯是错别字，语义不通'):
            doc=parse_document(text)
            ops=(EditOp('rewrite',('s000001',),('争执。',),'收敛',()),)+extra
            rendered=compile_edits(doc,ops)
            source_quote=doc.units[4].text.strip()
            p=dict(kind='coherence',source_ids=['s000005'],output_ids=['o000005'],evidence='医院的顾灯。',related_quote=related,reason=reason)
            response=dict(verdicts=[dict(finding_id='p1',verdict='confirmed',reason='原文中该句有词语错误，成稿仍如此',source_quote=source_quote,output_quote='医院的顾灯。')])
            with patch('novel_monitor.scene_model._request_json',return_value=response):
                c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1)
                return _verify_blocking(doc,rendered,(),[p],c)[0]
        inherited=verify(original)
        self.assertEqual(inherited['kind'],'dismissed_review_finding')
        self.assertEqual(inherited['verification']['method'],'source_lexical_identity')
        changed=EditOp('rewrite',('s000005',),('医院的顾灯。',),'引入错字',())
        self.assertEqual(verify(original.replace('顾灯','灯光'),(changed,))['kind'],'coherence')
        neighbor=EditOp('rewrite',('s000004',),('她从未离开家。',),'邻接变化',())
        self.assertEqual(verify(original,(neighbor,))['kind'],'coherence')
        self.assertEqual(verify(original,related='争执。')['kind'],'coherence')
        self.assertEqual(verify(original,reason='人物关系和时间自相矛盾')['kind'],'coherence')

    def test_repair_budget_splits_without_losing_sources_evidence_or_existing_atoms(self):
        from novel_monitor.scene_document import EditOp
        from novel_monitor.scene_review import _edit_groups
        doc=parse_document('\n'.join(f'第{i}行。原场景细节。' for i in range(140)))
        ids=[u.id for u in doc.units]
        atom=EditOp('compress',tuple(ids[68:73]),('已接受压缩。',),'原编辑',())
        problem=dict(kind='coherence',source_ids=ids,output_ids=['o000001'],evidence='原场景细节',
                     related_quote=doc.original,reason='修复因果与指代。'*80)
        snapshot=copy.deepcopy(problem);seen=[];calls=[]
        def provider(**kw):
            p=kw['payload'];calls.append(p)
            self.assertLessEqual(len(json.dumps(p,ensure_ascii=False)),8000)
            self.assertEqual(p['problems'][0]['related_quote'],doc.original)
            self.assertTrue(set(p['problems'][0]['source_ids'])<=set(p['required_target_ids']))
            self.assertNotIn('output_ids',p['problems'][0])
            seen.extend(p['required_target_ids'])
            return dict(operations=[],checked_source_count=p['required_target_count'],checked_scope=p['scope_id'])
        with tempfile.TemporaryDirectory() as tmp,patch('novel_monitor.scene_model._request_json',side_effect=provider):
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1,checkpoint_dir=Path(tmp));c.max_chars=8000;c.extra_rules=''
            ops=_edit_groups(doc,(atom,),[dict(source_ids=ids,reason='修补')],(),c,8000,True,[problem])
        self.assertEqual(seen,ids)
        self.assertGreater(len(calls),1)
        self.assertEqual(sum(set(atom.source_ids)<=set(p['required_target_ids']) for p in calls),1)
        self.assertEqual(compile_edits(doc,ops).text,compile_edits(doc,(atom,)).text)
        self.assertEqual(problem,snapshot)

    def test_newly_discovered_old_invention_cannot_survive_prior_version_restore(self):
        from novel_monitor.scene_document import EditOp
        from novel_monitor.scene_review import _restore_operations,_prior_without_repeated_inventions
        doc=parse_document('辱骂。\n过渡。\n她介入婚姻。\n后来。\n结局。')
        safe=EditOp('rewrite',('s000001',),('争执。',),'降险',())
        invented=EditOp('rewrite',('s000003',),('她住在他名下的房子。',),'错误推断',())
        bad_end=EditOp('rewrite',('s000005',),('她又多了一个孩子。',),'新增',())
        previous=(safe,invented);current=previous+(bad_end,)
        problems=[dict(kind='unsupported_event',source_ids=['s000003'],evidence='她住在他名下的房子。',reason='无产权证据'),
                  dict(kind='unsupported_event',source_ids=['s000005'],evidence='她又多了一个孩子。',reason='新增')]
        restored,_=_restore_operations(doc,current,previous,problems)
        self.assertEqual(compile_edits(doc,restored).text,'争执。\n过渡。\n她介入婚姻。\n后来。\n结局。')
        self.assertEqual(_prior_without_repeated_inventions(current,previous,problems),(safe,))

    def test_source_quote_retry_selects_known_id_instead_of_recopying_bad_quote(self):
        from novel_monitor.scene_review import _find_scenes
        doc=parse_document('他骂道：“贱人。”\n结局。')
        def provider(**kw):
            evidence=dict(id='s1')
            if '证据纠正请求：evidence每项只返回id' not in kw['prompt']:
                evidence['span']='并不存在的引文'
            return dict(scenes=[dict(source_ids=['s1'],categories=['abuse'],evidence=[evidence],reason='辱骂')],checked_source_ids=['s1','s2'])
        with tempfile.TemporaryDirectory() as tmp,patch('novel_monitor.scene_model._request_json',side_effect=provider):
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1,checkpoint_dir=Path(tmp))
            scenes=_find_scenes(doc,c,12000,'')
        self.assertEqual(scenes[0]['evidence'][0]['span'],doc.units[0].text.strip())
        self.assertEqual(c.audit_calls,2)

    def test_source_and_content_reference_only_evidence_uses_actual_units(self):
        from novel_monitor.scene_review import _find_scenes,review_scene_candidate
        doc=parse_document('他骂道：“贱人。”\n结局。')
        def provider(**kw):
            p=kw['payload']
            if p['task']=='find_scenes':
                return dict(scenes=[dict(source_ids=['s1'],categories=['abuse'],evidence=[dict(id='s1')],reason='辱骂')],
                    checked_source_ids=['s1','s000001','s2'])
            return dict(risks=[dict(id='o1',category='abuse',reason='辱骂')],checked_output_ids=['o1','o2'])
        with tempfile.TemporaryDirectory() as tmp,patch('novel_monitor.scene_model._request_json',side_effect=provider):
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1,checkpoint_dir=Path(tmp))
            scenes=_find_scenes(doc,c,12000,'')
            risks=review_scene_candidate(doc,compile_edits(doc,()),(),c)
        self.assertEqual(scenes[0]['evidence'][0]['span'],doc.units[0].text.strip())
        self.assertEqual(risks[0]['span'],doc.units[0].text.strip())

    def test_bad_coherence_quote_is_rejected_then_explicit_unit_reference_can_correct_it(self):
        from novel_monitor.scene_document import EditOp
        from novel_monitor.scene_review import review_scene_candidate
        doc=parse_document('她离开。')
        rendered=compile_edits(doc,(EditOp('rewrite',('s000001',),('那场火后她离开。',),'错误添加',()),))
        count=0
        def provider(**kw):
            nonlocal count
            p=kw['payload']
            if p['task']=='audit_additions':
                return dict(additions=[],checked_output_count=p['output_count'],checked_candidate_sha256=p['candidate_sha256'])
            if p['task']=='verify_findings':
                return dict(verdicts=[dict(finding_id=f['id'],verdict='confirmed',reason='无来源的火灾',source_quote='她离开。',output_quote='那场火后她离开。') for f in p['findings']])
            if p['task']=='audit_coherence':
                count+=1;issue=dict(output_ids=['o1'],reason='火灾无来源')
                if count==1:issue['output_quote']='编造的引用'
                return dict(issues=[issue],checked_output_ids=['o1'])
            return dict(risks=[],checked_output_ids=['o1'])
        with tempfile.TemporaryDirectory() as tmp,patch('novel_monitor.scene_model._request_json',side_effect=provider):
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1,checkpoint_dir=Path(tmp))
            problems=review_scene_candidate(doc,rendered,(),c)
            self.assertEqual(len(list(Path(tmp).glob('*.invalid-*.json'))),1)
        self.assertEqual(count,2)
        self.assertEqual(problems[0]['evidence'].strip(),rendered.text.strip())

    def test_coherent_new_event_is_detected_against_original_even_when_story_reads_smoothly(self):
        from novel_monitor.scene_document import CoreFact,EditOp
        from novel_monitor.scene_review import review_scene_candidate
        doc=parse_document('母亲去世。\n她离开。')
        rendered=compile_edits(doc,(EditOp('rewrite',('s000002',),('她离开。','她又生下另一个孩子。'),'错误新增',()),))
        facts=(CoreFact('f1','event','母亲去世',('s000001',),'母亲去世。'),)
        for mode in ('valid','unknown','incomplete'):
            def provider(**kw):
                p=kw['payload'];task=p['task']
                if task=='audit_additions':
                    self.assertEqual(p['original_context'],doc.original)
                    return dict(additions=[dict(output_id=3 if mode!='unknown' else 999,kind='new_outcome',reason='原文没有再次生育的结果')],
                        checked_output_count=p['output_count']-(mode=='incomplete'),checked_candidate_sha256=p['candidate_sha256'])
                if task=='verify_findings':
                    return dict(verdicts=[dict(finding_id=f['id'],verdict='confirmed',reason='确属新增结果',source_quote='她离开。',output_quote='她又生下另一个孩子。') for f in p['findings']])
                return dict({'issues' if task=='audit_coherence' else 'risks':[]},checked_output_ids=p['required_checked_output_ids'])
            with tempfile.TemporaryDirectory() as tmp,patch('novel_monitor.scene_model._request_json',side_effect=provider):
                c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1,checkpoint_dir=Path(tmp))
                if mode=='valid':
                    problems=review_scene_candidate(doc,rendered,facts,c)
                    self.assertTrue(any(p['kind']=='unsupported_event' and '孩子' in p['evidence'] for p in problems))
                else:
                    with self.assertRaises(SceneProtocolError):review_scene_candidate(doc,rendered,facts,c)

    def test_factual_difference_pages_cover_all_changed_output_without_truncating_source(self):
        from novel_monitor.scene_document import EditOp
        from novel_monitor.scene_review import _addition_payloads,_output_units
        doc=parse_document('\n'.join('原文'*15+str(i)+'。' for i in range(100)))
        ops=(EditOp('compress',tuple(u.id for u in doc.units),tuple('成稿'*30+str(i)+'。' for i in range(100)),'改写',()),)
        rendered=compile_edits(doc,ops);output=_output_units(rendered)
        pages=_addition_payloads(doc,rendered,output,8000)
        self.assertGreater(len(pages),1)
        self.assertTrue(all(p['original_context']==doc.original and len(json.dumps(p,ensure_ascii=False))<=8000 for p in pages))
        self.assertEqual(sum(p['output_count'] for p in pages),len(output))
        covered={int(line.split('|',1)[0]) for p in pages for line in p['output_text'].splitlines()}
        self.assertEqual(covered,set(range(1,len(output)+1)))

    def test_whole_story_core_verdict_uses_current_output_references_and_rejects_unknown_ids(self):
        from novel_monitor.scene_document import CoreFact,EditOp
        from novel_monitor.scene_review import review_scene_candidate
        doc=parse_document('她被拘禁。\n'+('背景。\n'*80)+'她最终离开。')
        rendered=compile_edits(doc,(EditOp('rewrite',('s000001',),('她被强行关在车内。',),'概括',()),))
        facts=(CoreFact('f1','event','遭到拘禁',tuple(u.id for u in doc.units[:70]),'她被拘禁。'),)
        for ref in (1,'o0001',9999):
            def request(**kw):
                p=kw['payload']
                if p['task']=='audit_core_global':
                    self.assertIn('1|她被强行关在车内。',p['output_text'])
                    self.assertNotIn('source_ids',p['facts'][0])
                    return dict(verdicts=[dict(fact_id='f1',verdict='present',output_ids=[ref],reason='强行关押的性质保留')])
                self.assertNotEqual(p['task'],'audit_core')
                return dict({'issues' if p['task']=='audit_coherence' else 'risks':[]},checked_output_ids=p['required_checked_output_ids'])
            with tempfile.TemporaryDirectory() as tmp,patch('novel_monitor.scene_model._request_json',side_effect=with_no_added_facts(request)):
                c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1,checkpoint_dir=Path(tmp));c.max_chars=4000
                if ref==9999:
                    with self.assertRaises(SceneProtocolError):review_scene_candidate(doc,rendered,facts,c)
                else:
                    self.assertEqual(review_scene_candidate(doc,rendered,facts,c),())

    def test_quote_retry_can_select_a_program_supplied_exact_short_source_fragment(self):
        from novel_monitor.scene_review import parse_core_facts
        doc=parse_document('同事说小林出差的沿海城市遭遇风暴，可能尚未取得联系。')
        requests=[]
        def request(**kw):
            p=kw['payload'];requests.append(p)
            quote='同事说小林旅途中遇上了暴雨。' if len(requests)==1 else p['protocol_hints'][0]['exact_sources'][0]['exact_quote_options'][0]
            return dict(facts=[dict(id='f1',kind='event',statement='同事声称小林出差地有风暴',source_ids=['s000001'],evidence=quote)])
        with tempfile.TemporaryDirectory() as tmp,patch('novel_monitor.scene_model._request_json',side_effect=with_no_added_facts(request)):
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1,checkpoint_dir=Path(tmp))
            facts=c.ask('extract_core','test',dict(source=[dict(id='s000001',text=doc.original)]),lambda raw:parse_core_facts(raw,doc))
        self.assertIn(facts[0].evidence,doc.original)
        self.assertEqual(len(requests),2)

    def test_composite_fact_supports_multiple_exact_output_quotes_without_joining_them(self):
        from novel_monitor.scene_document import CoreFact,EditOp
        from novel_monitor.scene_review import review_scene_candidate
        doc=parse_document('手指受伤，手腕有印。\n结束。')
        rendered=compile_edits(doc,(EditOp('rewrite',('s000001',),('她手指受伤。','稍后发现手腕有印。'),'拆段',()),))
        facts=(CoreFact('f1','event','手指受伤且手腕有印',('s000001',),'手指受伤，手腕有印。'),)
        def request(**kw):
            p=kw['payload']
            if p['task']=='audit_core':
                return dict(verdicts=[dict(fact_id='f1',verdict='present',output_ids=['o000001','o000002'],
                    output_quotes=[dict(output_id='o000001',span='手指受伤'),dict(output_id='o000002',span='手腕有印')],reason='两个结果均保留')])
            return dict({'issues' if p['task']=='audit_coherence' else 'risks':[]},checked_output_ids=p['required_checked_output_ids'])
        with tempfile.TemporaryDirectory() as tmp,patch('novel_monitor.scene_model._request_json',side_effect=with_no_added_facts(request)):
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1,checkpoint_dir=Path(tmp))
            self.assertEqual(review_scene_candidate(doc,rendered,facts,c),())

    def test_unresolved_local_rollback_restores_previous_valid_round_and_keeps_earlier_safe_edit(self):
        from novel_monitor.scene_document import EditOp
        source='主线。\n衔接。\n可删细节。\n过渡。\n结局。\n辱骂句。'
        safe=EditOp('delete',('s000003',),(),'删非必要细节',())
        wrong=EditOp('rewrite',('s000005',),('错改结局。',),'不安全改动',())
        def edit(document,ops,scenes,facts,client,max_chars,repair=False,problems=()):
            return (safe,) if scenes[0].get('initial') else (safe,wrong)
        def review(document,rendered,facts,client):
            # A distant failure is incompletely localized to the unchanged
            # setup, so restoring only that neighborhood cannot fix the end.
            content=dict(kind='content',source_ids=['s000006'],id='o6',category='abuse',span='辱骂句',reason='用语风险')
            if '错改结局' in rendered.text:
                return (dict(kind='coherence',source_ids=['s000001'],reason='远处结局与开头矛盾'),content)
            return (content,)
        with patch('novel_monitor.scene_review.extract_core_facts',return_value=()), \
             patch('novel_monitor.scene_review._find_scenes',return_value=[dict(source_ids=['s000003'],initial=True)]), \
             patch('novel_monitor.scene_review._edit_groups',side_effect=edit), \
             patch('novel_monitor.scene_model._request_json',side_effect=with_no_added_facts(lambda **kw:self.fail('非预期接口阶段'))), \
             patch('novel_monitor.scene_review.review_scene_candidate',side_effect=review):
            result=review_scenes_and_rewrite(api_key='test',model='test',url='https://test.invalid',original=source,
                current=source,extra_rules='',banned_terms=(),replacements={},max_chars=12000,timeout=1,checkpoint_dir=None)
        self.assertNotIn('可删细节',result.text)
        self.assertNotIn('错改结局',result.text)
        self.assertIn('结局。',result.text)
        self.assertEqual(result.review_status,'fidelity_fallback')
        self.assertTrue(any(a['outcome']=='restored_previous_valid' for a in result.risk_actions))

    def test_explicit_insult_is_targeted_and_reported_even_when_model_misses_it(self):
        from novel_monitor.scene_review import _find_scenes,review_scene_candidate
        doc=parse_document('他骂道：“给脸不要脸。”\n最终离开。')
        def request(**kw):
            p=kw['payload']
            if p['task']=='find_scenes':
                return dict(scenes=[],checked_source_ids=p['required_checked_source_ids'])
            return dict(risks=[],checked_output_ids=p['required_checked_output_ids'])
        with tempfile.TemporaryDirectory() as tmp,patch('novel_monitor.scene_model._request_json',side_effect=with_no_added_facts(request)):
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1,checkpoint_dir=Path(tmp))
            scenes=_find_scenes(doc,c,12000,'')
            problems=review_scene_candidate(doc,compile_edits(doc,()),(),c)
        self.assertTrue(any(s['source_ids']==['s000001'] for s in scenes))
        self.assertTrue(any(p['span']=='给脸不要脸' and p['origin']=='rule_precheck' for p in problems))

    def test_remote_coherence_evidence_links_the_edited_cause_for_rollback(self):
        from novel_monitor.scene_document import EditOp
        from novel_monitor.scene_review import review_scene_candidate,_restore_operations
        doc=parse_document('唯一物件是红伞。\n'+('途中。\n'*12)+'她打开红伞。')
        ops=(EditOp('rewrite',('s000001',),('唯一物件是红箱。',),'错误改名',()),)
        rendered=compile_edits(doc,ops)
        def request(**kw):
            p=kw['payload']
            if p['task']=='verify_findings':
                return dict(verdicts=[dict(finding_id=f['id'],verdict='confirmed',reason='物件矛盾',
                    source_quote='唯一物件是红伞。',output_quote='唯一物件是红箱。') for f in p['findings']])
            if p['task']=='audit_coherence':
                return dict(issues=[dict(output_ids=[p['output'][-1]['id']],output_quote='她打开红伞。',
                    related_quote='唯一物件是红箱。',reason='唯一物件与后文不符')],checked_output_ids=p['required_checked_output_ids'])
            return dict(risks=[],checked_output_ids=p['required_checked_output_ids'])
        with tempfile.TemporaryDirectory() as tmp,patch('novel_monitor.scene_model._request_json',side_effect=with_no_added_facts(request)):
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1,checkpoint_dir=Path(tmp))
            problems=review_scene_candidate(doc,rendered,(),c)
        self.assertIn('s000001',problems[0]['source_ids'])
        self.assertIn(doc.units[-1].id,problems[0]['source_ids'])
        restored,_=_restore_operations(doc,ops,(),problems)
        self.assertEqual(compile_edits(doc,restored).text,doc.original)

    def test_nonadjacent_problem_sources_do_not_expand_repair_across_untouched_middle(self):
        from novel_monitor.scene_review import _target_groups
        doc=parse_document('\n'.join('中间段。' for _ in range(30)))
        groups=_target_groups(doc,[dict(source_ids=[doc.units[0].id,doc.units[-1].id])],(),12000)
        targeted={u.id for g in groups for u in g}
        self.assertIn(doc.units[0].id,targeted)
        self.assertIn(doc.units[-1].id,targeted)
        self.assertNotIn(doc.units[15].id,targeted)

    def test_whole_candidate_coherence_is_read_without_source_detail_comparison(self):
        from novel_monitor.scene_document import EditOp
        from novel_monitor.scene_review import review_scene_candidate
        doc=parse_document('她淋雨得了急性肺炎。\n后来她离开。')
        rendered=compile_edits(doc,(EditOp('rewrite',('s000001',),('她淋雨生了病。',),'概括',()),))
        def request(**kw):
            p=kw['payload']
            if p['task']=='audit_coherence':
                self.assertEqual(p['candidate_text'],rendered.text)
                self.assertNotIn('source',p)
                self.assertNotIn('change_ledger',p)
            return dict({'issues' if p['task']=='audit_coherence' else 'risks':[]},checked_output_ids=p['required_checked_output_ids'])
        with tempfile.TemporaryDirectory() as tmp,patch('novel_monitor.scene_model._request_json',side_effect=with_no_added_facts(request)):
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1,checkpoint_dir=Path(tmp))
            self.assertEqual(review_scene_candidate(doc,rendered,(),c),())

    def test_finding_verifier_uncertainty_stays_blocking_and_invented_quotes_cannot_dismiss(self):
        from novel_monitor.scene_document import EditOp
        from novel_monitor.scene_review import _verify_blocking
        doc=parse_document('她被拘禁。\n她离开。')
        rendered=compile_edits(doc,(EditOp('rewrite',('s000001',),('她被遗落。',),'错误',()),))
        problem=dict(kind='core_changed',source_ids=['s000001'],reason='事件改变')
        def response(verdict,quote):
            return dict(verdicts=[dict(finding_id='p1',verdict=verdict,reason='证据不足或错误',
                source_quote='她被拘禁。',output_quote=quote)])
        with tempfile.TemporaryDirectory() as tmp,patch('novel_monitor.scene_model._request_json',return_value=response('uncertain','')):
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1,checkpoint_dir=Path(tmp))
            self.assertEqual(_verify_blocking(doc,rendered,(),[problem],c)[0]['kind'],'core_changed')
        with patch('novel_monitor.scene_model._request_json',return_value=response('unsupported','不存在的话。')):
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1)
            with self.assertRaises(SceneProtocolError):
                _verify_blocking(doc,rendered,(),[problem],c)
        with patch('novel_monitor.scene_model._request_json',return_value=response('confirmed','')):
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1)
            self.assertEqual(_verify_blocking(doc,rendered,(),[problem],c)[0]['kind'],'core_changed')
        with patch('novel_monitor.scene_model._request_json',return_value=response('confirmed','不存在的话。')):
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1)
            preserved=_verify_blocking(doc,rendered,(),[problem],c)[0]
            self.assertEqual(preserved['kind'],'core_changed')
            self.assertEqual(preserved['verification']['verdict'],'uncertain')
            self.assertEqual(preserved['verification']['output_quote'],'')

    def test_independent_finding_check_rejects_detail_demand_but_keeps_true_event_loss(self):
        from novel_monitor.scene_document import CoreFact,EditOp
        from novel_monitor.scene_review import _verify_blocking
        doc=parse_document('母亲遭纵火去世。\n血液细节。\n她最终离开。')
        rendered=compile_edits(doc,(EditOp('rewrite',('s000001',),('母亲病逝。',),'错误',()),
            EditOp('delete',('s000002',),(),'删除特写',())))
        problems=[dict(kind='core_changed',source_ids=['s000001'],reason='改变死因',fact_id='f1'),
            dict(kind='coherence',source_ids=['s000002'],reason='删血液削弱共情力度')]
        facts=(CoreFact('f1','event','母亲遭纵火去世',('s000001',),'母亲遭纵火去世。'),)
        def request(**kw):
            p=kw['payload']
            self.assertEqual(p['original_context'],doc.original)
            return dict(verdicts=[dict(finding_id=f['id'],verdict='confirmed' if f['kind']=='core_changed' else 'unsupported',
                reason='改变主要死因' if f['kind']=='core_changed' else '只删感官细节',source_quote='母亲遭纵火去世。',output_quote='母亲病逝。') for f in p['findings']])
        with tempfile.TemporaryDirectory() as tmp,patch('novel_monitor.scene_model._request_json',side_effect=with_no_added_facts(request)):
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1,checkpoint_dir=Path(tmp));c.max_chars=12000
            result=_verify_blocking(doc,rendered,facts,problems,c)
        self.assertEqual(result[0]['kind'],'core_changed')
        self.assertEqual(result[1]['kind'],'dismissed_review_finding')
        self.assertEqual(result[1]['verification']['verdict'],'unsupported')

    def test_local_fallback_does_not_cancel_second_round_for_other_content_flags(self):
        from novel_monitor.scene_document import EditOp
        doc=parse_document('她被拘禁。\n过渡。\n过渡二。\n过渡三。\n辱骂句。')
        wrong=EditOp('rewrite',('s000001',),('她被遗落。',),'错误改事件',())
        safe=EditOp('rewrite',('s000005',),('双方发生争执。',),'删除辱骂',())
        def edit(document,ops,scenes,facts,client,max_chars,repair=False,problems=()):
            return (wrong,) if repair or scenes[0].get('initial') else (safe,)
        def review(document,rendered,facts,client):
            issues=[]
            if '遗落' in rendered.text:
                issues.append(dict(kind='core_changed',source_ids=['s000001'],reason='拘禁变遗落'))
            if '辱骂句' in rendered.text:
                issues.append(dict(kind='content',source_ids=['s000005'],id='o000005',category='abuse',span='辱骂句',reason='明确辱骂'))
            return tuple(issues)
        with patch('novel_monitor.scene_review.extract_core_facts',return_value=()), \
             patch('novel_monitor.scene_review._find_scenes',return_value=[dict(source_ids=['s000001'],initial=True)]), \
             patch('novel_monitor.scene_review._edit_groups',side_effect=edit) as edits, \
             patch('novel_monitor.scene_model._request_json',side_effect=with_no_added_facts(lambda **kw:self.fail('非预期接口阶段'))), \
             patch('novel_monitor.scene_review.review_scene_candidate',side_effect=review):
            result=review_scenes_and_rewrite(api_key='test',model='test',url='https://test.invalid',original=doc.original,
                current=doc.original,extra_rules='',banned_terms=(),replacements={},max_chars=12000,timeout=1,checkpoint_dir=None)
        self.assertIn('她被拘禁。',result.text)
        self.assertIn('双方发生争执。',result.text)
        self.assertNotIn('辱骂句',result.text)
        self.assertEqual(edits.call_count,3)

    def test_unchanged_fact_spans_are_proved_exact_while_changed_facts_are_audited(self):
        from novel_monitor.scene_document import CoreFact,EditOp
        from novel_monitor.scene_review import review_scene_candidate
        doc=parse_document('她离婚。\n伤害细节很多。\n最终离开。')
        facts=(CoreFact('f1','event','离婚',('s000001',),'她离婚。'),
               CoreFact('f2','event','受到伤害',('s000002',),'伤害细节很多。'),
               CoreFact('f3','ending','离开',('s000003',),'最终离开。'))
        rendered=compile_edits(doc,(EditOp('rewrite',('s000002',),('她受到伤害。',),'概括',()),))
        checked=[]
        def request(**kw):
            p=kw['payload']
            if p['task']=='audit_core':
                checked.extend(p['required_fact_ids'])
                return dict(verdicts=[dict(fact_id=f['id'],verdict='present',output_ids=['o000002'],output_quote='她受到伤害。',reason='事件保留') for f in p['facts']])
            return dict({'issues' if p['task']=='audit_coherence' else 'risks':[]},checked_output_ids=p['required_checked_output_ids'])
        with tempfile.TemporaryDirectory() as tmp,patch('novel_monitor.scene_model._request_json',side_effect=with_no_added_facts(request)):
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1,checkpoint_dir=Path(tmp))
            self.assertEqual(review_scene_candidate(doc,rendered,facts,c),())
        self.assertEqual(checked,['f2'])

    def test_scene_finding_quote_failure_supplies_exact_source_for_retry(self):
        from novel_monitor.scene_review import _find_scenes
        doc=parse_document('他喊：“伤害别人。”\n结局。')
        seen=[]
        def request(**kw):
            p=kw['payload'];seen.append(p)
            quote='他喊：‘伤害别人。’' if len(seen)==1 else '伤害别人'
            return dict(scenes=[dict(source_ids=['s000001'],categories=['violence'],
                evidence=[dict(id='s000001',span=quote)],reason='危险指令')],
                checked_source_ids=p['required_checked_source_ids'])
        with tempfile.TemporaryDirectory() as tmp,patch('novel_monitor.scene_model._request_json',side_effect=with_no_added_facts(request)):
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1,checkpoint_dir=Path(tmp))
            result=_find_scenes(doc,c,12000,'')
        self.assertEqual(len(result),1)
        self.assertEqual(seen[1]['protocol_hints'][0]['exact_sources'][0]['text'],'他喊：“伤害别人。”')

    def test_content_quote_failure_supplies_exact_current_unit_for_retry(self):
        from novel_monitor.scene_review import review_scene_candidate
        doc=parse_document('他喊：“伤害别人。”\n结局。')
        seen=[]
        def request(**kw):
            p=kw['payload'];seen.append(p)
            quote='他喊：‘伤害别人。’' if len(seen)==1 else '伤害别人'
            return dict(risks=[dict(id=p['output'][0]['id'],category='violence',span=quote,reason='危险指令')],
                checked_output_ids=p['required_checked_output_ids'])
        with tempfile.TemporaryDirectory() as tmp,patch('novel_monitor.scene_model._request_json',side_effect=with_no_added_facts(request)):
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1,checkpoint_dir=Path(tmp))
            result=review_scene_candidate(doc,compile_edits(doc,()),(),c)
        self.assertEqual(len(result),1)
        self.assertEqual(seen[1]['protocol_hints'][0]['exact_output'][0]['text'],'他喊：“伤害别人。”')

    def test_unchanged_source_has_identity_proof_but_content_audit_is_still_required(self):
        from novel_monitor.scene_review import review_scene_candidate
        doc=parse_document("危险细节。\n结局。")
        def request(**kw):
            p=kw["payload"]
            self.assertEqual(p["task"],"audit_content")
            return dict(risks=[dict(id=p["output"][0]["id"],category="violence",span="危险细节",reason="具体刺激内容")],checked_output_ids=[u["id"] for u in p["output"]])
        with tempfile.TemporaryDirectory() as tmp,patch("novel_monitor.scene_model._request_json",side_effect=with_no_added_facts(request)):
            c=SceneModelClient(api_key="test",model="test",url="https://test.invalid",timeout=1,checkpoint_dir=Path(tmp))
            problems=review_scene_candidate(doc,compile_edits(doc,()),(),c)
        self.assertEqual(len(problems),1)
        self.assertEqual(problems[0]["kind"],"content")

    def test_repair_does_not_duplicate_the_same_long_problem_as_scene_reason(self):
        from novel_monitor.scene_review import _edit_groups
        doc=parse_document("甲。\n乙。\n结局。")
        ids=[u.id for u in doc.units];reason="需要修复前后指代。"*650
        problem=dict(kind="coherence",source_ids=ids,evidence="甲",reason=reason)
        def request(**kw):
            p=kw["payload"]
            self.assertEqual(p["problems"][0]["reason"],reason)
            return {"operations":[raw_op("keep",ids)]}
        with tempfile.TemporaryDirectory() as tmp,patch("novel_monitor.scene_model._request_json",side_effect=with_no_added_facts(request)):
            c=SceneModelClient(api_key="test",model="test",url="https://test.invalid",timeout=1,checkpoint_dir=Path(tmp));c.max_chars=12000;c.extra_rules=""
            ops=_edit_groups(doc,(),[dict(source_ids=ids,reason=reason)],(),c,12000,True,[problem])
        self.assertEqual(compile_edits(doc,ops).text,doc.original)

    def test_repair_requests_include_only_problems_in_the_target_scene(self):
        from novel_monitor.scene_review import _edit_groups
        doc=parse_document("\n".join(f"第{i}段。" for i in range(20)))
        problems=[dict(kind="coherence",source_ids=[doc.units[0].id],evidence="第0段",reason="本场景问题")]
        problems += [dict(kind="coherence",source_ids=[doc.units[-1].id],evidence="第19段",reason="远处问题"*2000) for _ in range(5)]
        def request(**kw):
            p=kw["payload"]
            self.assertEqual(len(p["problems"]),1)
            return {"operations":[raw_op("keep",[u["id"] for u in p["source"]])]}
        with tempfile.TemporaryDirectory() as tmp,patch("novel_monitor.scene_model._request_json",side_effect=with_no_added_facts(request)):
            c=SceneModelClient(api_key="test",model="test",url="https://test.invalid",timeout=1,checkpoint_dir=Path(tmp));c.max_chars=12000;c.extra_rules=""
            ops=_edit_groups(doc,(),[dict(source_ids=[doc.units[0].id])],(),c,12000,True,problems)
        self.assertEqual(compile_edits(doc,ops).text,doc.original)

    def test_scene_editor_delta_keeps_current_and_ordinary_paragraphs_exact(self):
        from novel_monitor.scene_review import _edit_groups
        doc=parse_document('旧风险。\n贱人！\n普通时间地点。\n结局。')
        ids=[u.id for u in doc.units]
        prior=parse_edit_ops({'operations':[raw_op('rewrite',[ids[0]],['已减弱的表达。'])]},doc,[ids[0]],())
        def request(**kw):
            p=kw['payload']
            return dict(operations=[raw_op('rewrite',[ids[1]],['别这样说。'])],
                checked_source_count=p['required_target_count'],checked_scope=p['scope_id'])
        with tempfile.TemporaryDirectory() as tmp,patch('novel_monitor.scene_model._request_json',side_effect=request):
            c=SceneModelClient(api_key='test',model='test',url='https://test.invalid',timeout=1,checkpoint_dir=Path(tmp))
            c.max_chars=12000;c.extra_rules=''
            ops=_edit_groups(doc,prior,[dict(source_ids=ids,reason='辱骂')],(),c,12000)
        self.assertEqual(compile_edits(doc,ops).text,'已减弱的表达。\n别这样说。\n普通时间地点。\n结局。')

    def test_coherence_problem_quotes_current_output_with_explicit_field(self):
        from novel_monitor.scene_document import EditOp
        from novel_monitor.scene_review import review_scene_candidate
        doc=parse_document("她离开。")
        rendered=compile_edits(doc,(EditOp("rewrite",("s000001",),("那场火之后她离开。",),"修改",()),))
        def request(**kw):
            p=kw["payload"]
            if p['task']=='verify_findings':
                return dict(verdicts=[dict(finding_id=f['id'],verdict='confirmed',reason='新增无依据事件',
                    source_quote=p['source'][0]['text'],output_quote=p['output'][0]['text']) for f in p['findings']])
            if p["task"]=="audit_coherence":
                return dict(issues=[dict(output_ids=["o000001"],output_quote="那场火",reason="新增无依据引用")],checked_output_ids=["o000001"])
            return dict(risks=[],checked_output_ids=["o000001"])
        with tempfile.TemporaryDirectory() as tmp,patch("novel_monitor.scene_model._request_json",side_effect=with_no_added_facts(request)):
            c=SceneModelClient(api_key="test",model="test",url="https://test.invalid",timeout=1,checkpoint_dir=Path(tmp))
            problems=review_scene_candidate(doc,rendered,(),c)
        self.assertTrue(any(p["kind"]=="coherence" and p["evidence"]=="那场火" for p in problems))

    def test_core_verdict_explicit_output_quote_supports_changed_wording(self):
        from novel_monitor.scene_document import CoreFact,EditOp
        from novel_monitor.scene_review import review_scene_candidate
        doc=parse_document("复婚信用打卡表。")
        rendered=compile_edits(doc,(EditOp("rewrite",("s000001",),("一份记录还款的表格。",),"概括",()),))
        facts=(CoreFact("f1","setting","存在记录还款的表格",("s000001",),"复婚信用打卡表。"),)
        def request(**kw):
            p=kw["payload"]
            if p["task"]=="audit_core":
                return dict(verdicts=[dict(fact_id="f1",verdict="present",output_ids=["o000001"],output_quote="一份记录还款的表格。",reason="主要功能保留")])
            return dict({"issues" if p["task"]=="audit_coherence" else "risks":[]},checked_output_ids=[u["id"] for u in p["output"]])
        with tempfile.TemporaryDirectory() as tmp,patch("novel_monitor.scene_model._request_json",side_effect=with_no_added_facts(request)):
            c=SceneModelClient(api_key="test",model="test",url="https://test.invalid",timeout=1,checkpoint_dir=Path(tmp))
            self.assertEqual(review_scene_candidate(doc,rendered,facts,c),())

    def test_core_verdict_correction_quotes_rendered_text_instead_of_old_source(self):
        from novel_monitor.scene_document import CoreFact,EditOp
        from novel_monitor.scene_review import review_scene_candidate
        doc=parse_document("复婚信用打卡表。")
        rendered=compile_edits(doc,(EditOp("rewrite",("s000001",),("一份记录还款的表格。",),"概括",()),))
        facts=(CoreFact("f1","setting","存在一份记录还款的表格",("s000001",),"复婚信用打卡表。"),)
        def request(**kw):
            p=kw["payload"]
            if p["task"]=="audit_core":
                evidence="一份记录还款的表格。" if p.get("protocol_hints") else "复婚信用打卡表。"
                return dict(verdicts=[dict(fact_id="f1",verdict="present",output_ids=["o000001"],evidence=evidence,reason="功能含义保留")])
            return dict({"issues" if p["task"]=="audit_coherence" else "risks":[]},checked_output_ids=[u["id"] for u in p["output"]])
        with tempfile.TemporaryDirectory() as tmp,patch("novel_monitor.scene_model._request_json",side_effect=with_no_added_facts(request)):
            c=SceneModelClient(api_key="test",model="test",url="https://test.invalid",timeout=1,checkpoint_dir=Path(tmp))
            self.assertEqual(review_scene_candidate(doc,rendered,facts,c),())

    def test_overlapping_risk_marks_become_one_continuous_scene_without_edit_overlap(self):
        from novel_monitor.scene_review import _find_scenes
        doc=parse_document("危险细节甲。\n衔接。\n危险细节乙。\n收尾。")
        raw=dict(scenes=[dict(source_ids=["s000001","s000003"],categories=["violence"],
                  evidence=[dict(id="s000001",span="危险细节甲")],reason="场景刺激细节"),
                  dict(source_ids=["s000002","s000003","s000004"],categories=["abuse"],
                  evidence=[dict(id="s000003",span="危险细节乙")],reason="同场景羞辱")],
                  checked_source_ids=[u.id for u in doc.units])
        with tempfile.TemporaryDirectory() as tmp,patch("novel_monitor.scene_model._request_json",return_value=raw):
            c=SceneModelClient(api_key="test",model="test",url="https://test.invalid",timeout=1,checkpoint_dir=Path(tmp))
            scenes=_find_scenes(doc,c,12000,"")
        self.assertEqual(len(scenes),1)
        self.assertEqual(scenes[0]["source_ids"],["s000001","s000002","s000003","s000004"])
        self.assertEqual(set(scenes[0]["categories"]),{"violence","abuse"})

    def test_rollback_closes_over_previous_compression_and_preserves_distant_good_edit(self):
        from novel_monitor.scene_document import EditOp
        from novel_monitor.scene_review import _restore_operations
        doc=parse_document("\n".join(f"第{i}段。" for i in range(20)))
        previous=(EditOp("compress",tuple(u.id for u in doc.units[:5]),("有效压缩。",),"压缩",()),)
        current=tuple(EditOp("rewrite",(u.id,),(f"候选{i}。",),"改写",()) for i,u in enumerate(doc.units))
        restored,ids=_restore_operations(doc,current,previous,[dict(source_ids=[doc.units[0].id]),dict(source_ids=[doc.units[-2].id])])
        result=compile_edits(doc,restored)
        self.assertIn("有效压缩。",result.text)
        self.assertIn("候选10。",result.text)
        self.assertNotIn("候选0。",result.text)
        self.assertNotIn(doc.units[10].id,ids)

    def test_remote_change_is_visible_and_cannot_reuse_old_coherence_pass(self):
        from novel_monitor.scene_document import EditOp
        from novel_monitor.scene_review import review_scene_candidate
        doc=parse_document("red umbrella\n"+("ordinary filler words. "*10+"\n")*40+"that umbrella")
        baseline=(EditOp("rewrite",(doc.units[5].id,),("ordinary harmless words.",),"局部改动",()),)
        ops=baseline+(EditOp("rewrite",(doc.units[0].id,),("red suitcase",),"改动",()),)
        seen=[]
        def request(**kw):
            p=kw["payload"]
            if p["task"]=="audit_coherence":
                seen.append(p)
                return dict(issues=[],checked_output_ids=[u["id"] for u in p["output"]])
            return dict(risks=[],checked_output_ids=[u["id"] for u in p["output"]])
        with tempfile.TemporaryDirectory() as tmp,patch("novel_monitor.scene_model._request_json",side_effect=with_no_added_facts(request)):
            c=SceneModelClient(api_key="test",model="test",url="https://test.invalid",timeout=1,checkpoint_dir=Path(tmp));c.max_chars=4000
            review_scene_candidate(doc,compile_edits(doc,baseline),(),c)
            seen.clear()
            review_scene_candidate(doc,compile_edits(doc,ops),(),c)
        tail=[p for p in seen if any("that umbrella" in u["text"] for u in p["output"])]
        self.assertTrue(tail,"后文必须重新核对，不能复用此前通过缓存")
        self.assertTrue(any("red suitcase" in json.dumps(p,ensure_ascii=False) for p in tail))

    def test_heavy_deletion_coherence_requests_stay_bounded_and_have_neighbors(self):
        from novel_monitor.scene_review import review_scene_candidate
        source="起因。\n" + "可删细节。"*50+"\n" + ("重复细节。"*50+"\n")*100 + "那场火之后，她离开。"
        doc=parse_document(source,600)
        ids=[u.id for u in doc.units[1:-1]]
        ops=parse_edit_ops({"operations":[raw_op("delete",ids)]},doc,ids,())
        rendered=compile_edits(doc,ops)
        seen=[]
        def request(**kw):
            p=kw["payload"]; seen.append(p)
            if p["task"]=="audit_coherence":
                return dict(issues=[],checked_output_ids=[u["id"] for u in p["output"]])
            return dict(risks=[],checked_output_ids=[u["id"] for u in p["output"]])
        with tempfile.TemporaryDirectory() as tmp, patch("novel_monitor.scene_model._request_json",side_effect=with_no_added_facts(request)):
            c=SceneModelClient(api_key="test",model="test",url="https://test.invalid",timeout=1,checkpoint_dir=Path(tmp))
            c.max_chars=12000
            review_scene_candidate(doc,rendered,(),c)
        self.assertTrue(seen)
        self.assertTrue(all(len(json.dumps(p,ensure_ascii=False))<=12000 for p in seen))
        self.assertTrue(all("context_before" in p and "context_after" in p for p in seen if p["task"]=="audit_coherence"))

    def run_review(self, source, editor=None, residual=False, broken=False, repair=False):
        def request(**kw):
            p = kw["payload"]
            task = p["task"]
            if task in {"extract_core", "verify_core"}:
                units = p["source"]
                value = {"facts": [dict(id="f1", kind="event", statement=source.splitlines()[0], source_ids=[units[0]["id"]], evidence=units[0]["text"].strip()),
                                   dict(id="f2", kind="ending", statement=source.splitlines()[-1], source_ids=[units[-1]["id"]], evidence=units[-1]["text"].strip())]}
                if task == "verify_core":
                    value["checked_source_ids"] = [u["id"] for u in units]
                return value
            if task == "find_scenes":
                units = p["source"]
                return dict(scenes=[dict(source_ids=[u["id"] for u in units], categories=["violence"], evidence=[dict(id=units[0]["id"], span=units[0]["text"].strip())], reason="删除非必要细节")] if editor else [], checked_source_ids=[u["id"] for u in units])
            if task == "edit_scene":
                units = p["source"]
                ops = [raw_op("keep", [u["id"]]) for u in units]
                if editor:
                    ops = editor(units, p.get("repair", False))
                return {"operations": ops}
            if task == "audit_core":
                output = p["output"]
                verdicts = []
                for f in p["facts"]:
                    match = next((u for u in output if f["evidence"] in u["text"]), None)
                    verdicts.append(dict(fact_id=f["id"], verdict="present" if match else "changed", output_ids=[match["id"]] if match else [], evidence=f["evidence"] if match else "", reason="核对事件性质和结局"))
                return {"verdicts": verdicts}
            if task == 'verify_findings':
                return dict(verdicts=[dict(finding_id=f['id'],verdict='confirmed',reason='确有事件丢失或指代断裂',
                    source_quote=p['source'][0]['text'],output_quote=p['output'][0]['text']) for f in p['findings']])
            output = p["output"]
            if broken and task == "audit_coherence":
                return {"issues": []}  # deliberately incomplete protocol
            if task == "audit_coherence":
                issues = []
                if any("那场火" in u["text"] for u in output) and not any("纵火" in u["text"] for u in output):
                    u = next(u for u in output if "那场火" in u["text"])
                    issues.append(dict(output_ids=[u["id"]], evidence="那场火", reason="删除后引用断裂"))
                return dict(issues=issues, checked_output_ids=[u["id"] for u in output])
            if task == "audit_content":
                return dict(risks=[dict(id=output[0]["id"], category="violence", span=output[0]["text"].strip(), reason="仍有事件风险")] if residual else [], checked_output_ids=[u["id"] for u in output])
            raise AssertionError(task)
        with tempfile.TemporaryDirectory() as tmp, patch("novel_monitor.scene_model._request_json", side_effect=with_no_added_facts(request)):
            return review_scenes_and_rewrite(api_key="test", model="test", url="https://test.invalid", original=source, current=source,
                extra_rules="", banned_terms=(), replacements={}, max_chars=12000, timeout=1, checkpoint_dir=Path(tmp))

    def test_detail_deletion_accepted_without_losing_event_or_ending(self):
        def edit(units, repair):
            return [raw_op("delete" if i == 1 else "keep", [u["id"]]) for i, u in enumerate(units)]
        result = self.run_review("他遭到伤害。\n鲜血细节。\n最终离婚。", edit)
        self.assertEqual(result.text, "他遭到伤害。\n最终离婚。")
        self.assertEqual(result.review_status, "complete")

    def test_core_event_changes_and_missing_ending_restore_source(self):
        for source, wrong in [("他们将她关在后备厢。\n最终断绝关系。", "他们把她留在路边。"),
                              ("母亲遭纵火去世。\n最终断绝关系。", "母亲病逝。"),
                              ("她受到侵害。\n最终离开。", "她与他聊天。")]:
            def edit(units, repair):
                return [raw_op("rewrite", [units[0]["id"]], [wrong])] + [raw_op("keep", [u["id"]]) for u in units[1:]]
            result = self.run_review(source, edit)
            self.assertEqual(result.text, source)
            self.assertTrue(any(a["outcome"] == "restored_core" for a in result.risk_actions))
        def delete_end(units, repair):
            return [raw_op("keep", [u["id"]]) for u in units[:-1]] + [raw_op("delete", [units[-1]["id"]])]
        self.assertEqual(self.run_review("争执。\n最终解除婚约。", delete_end).text, "争执。\n最终解除婚约。")

    def test_coherence_break_from_removed_setup_is_repaired(self):
        def edit(units, repair):
            return [raw_op("keep" if repair or i != 1 else "delete", [u["id"]]) for i, u in enumerate(units)]
        result = self.run_review("她受伤。\n起因是纵火。\n那场火之后，她离开。", edit)
        self.assertIn("纵火", result.text)
        self.assertIn(result.review_status, {"fidelity_repaired", "fidelity_fallback"})

    def test_low_risk_unchanged_and_content_residual_can_deliver(self):
        source = "两人分开。\n最终各自生活。"
        self.assertEqual(self.run_review(source).text, source)
        result = self.run_review(source, residual=True)
        self.assertEqual(result.review_status, "complete")
        self.assertTrue(result.remaining_risks)
        self.assertEqual(result.remaining_risks[0].handling,"uncertain")
        self.assertTrue(any(a["outcome"]=="residual_after_bounded_edits" for a in result.risk_actions))
        from novel_monitor.semantic_compliance import summarize_risks
        self.assertEqual(summarize_risks(result.remaining_risks)["supported_wording"],0)
        self.assertEqual(summarize_risks(result.remaining_risks)["uncertain"],1)

    def test_incomplete_mandatory_review_never_completes(self):
        def edit(units,repair):
            return [raw_op("delete" if i==1 else "keep",[u["id"]]) for i,u in enumerate(units)]
        with self.assertRaises(SceneProtocolError):
            self.run_review("她离开。\n非必要细节。\n最终相安。",editor=edit, broken=True)


class ProcessorSceneTests(unittest.TestCase):
    def test_old_scene_v2_unsent_stage_receives_current_review(self):
        from dataclasses import replace
        from test_pipeline import PipelineTests
        from novel_monitor import processor
        from novel_monitor.scene_document import EditOp
        doc=parse_document('风险描写。\n结局离开。')
        def result(ops):
            rendered=compile_edits(doc,ops)
            return SceneReviewResult(rendered.text,'complete',(),1,1,(),(),rendered.source_mapping,rendered.edit_actions)
        old=result(());new=result((EditOp('rewrite',('s000001',),('简洁描写。',),'删细节',()),))
        with tempfile.TemporaryDirectory() as tmp:
            config=replace(PipelineTests.rewrite_config(Path(tmp)),semantic_compliance_enabled=True)
            source=config.input_dir/'原书名.txt';source.write_text(doc.original,encoding='utf-8')
            with patch.object(processor,'review_and_rewrite',return_value=old),patch.object(processor,'send_file',side_effect=RuntimeError('webhook')):
                with self.assertRaisesRegex(RuntimeError,'webhook'):processor.process_file(source,config,logging.getLogger('test'))
            claimed=processor.recover_claimed_sources(config)[0]
            path=claimed.parent/'rewrite/staged.json';metadata=json.loads(path.read_text(encoding='utf-8'))
            metadata['semantic_pipeline_revision']='core-preserving-scene-edit-v2'
            path.write_text(json.dumps(metadata),encoding='utf-8')
            with patch.object(processor,'review_and_rewrite',return_value=new) as review,patch.object(processor,'send_file') as send:
                out=processor.process_file(claimed,config,logging.getLogger('test'))
            self.assertEqual(out.read_text(encoding='utf-8'),new.text)
            self.assertEqual(review.call_count,1);self.assertEqual(send.call_count,1)

    def test_old_scene_v2_already_sent_stage_does_not_resend_after_revision_change(self):
        from dataclasses import replace
        from test_pipeline import PipelineTests
        from novel_monitor import processor
        doc=parse_document('开篇。\n结局离开。');rendered=compile_edits(doc,())
        result=SceneReviewResult(rendered.text,'complete',(),1,0,(),(),rendered.source_mapping,rendered.edit_actions)
        with tempfile.TemporaryDirectory() as tmp:
            config=replace(PipelineTests.rewrite_config(Path(tmp)),semantic_compliance_enabled=True)
            source=config.input_dir/'原书名.txt';source.write_text(doc.original,encoding='utf-8')
            with patch.object(processor,'review_and_rewrite',return_value=result),patch.object(processor,'send_file'),patch.object(processor,'_publish_staged',side_effect=RuntimeError('queue interrupted')):
                with self.assertRaisesRegex(RuntimeError,'queue interrupted'):processor.process_file(source,config,logging.getLogger('test'))
            claimed=processor.recover_claimed_sources(config)[0]
            path=claimed.parent/'rewrite/staged.json';metadata=json.loads(path.read_text(encoding='utf-8'))
            metadata['semantic_pipeline_revision']='core-preserving-scene-edit-v2'
            path.write_text(json.dumps(metadata),encoding='utf-8')
            with patch.object(processor,'review_and_rewrite',side_effect=AssertionError('重复复核')),patch.object(processor,'send_file') as send:
                out=processor.process_file(claimed,config,logging.getLogger('test'))
            send.assert_not_called();self.assertEqual(out.read_text(encoding='utf-8'),doc.original)

    def test_final_literal_boundary_cannot_give_deleted_source_nonempty_output(self):
        from novel_monitor.processor import finalize_scene_text
        doc=parse_document("XAAAAAAADELETEMEBBBBBBBY",8)
        ops=parse_edit_ops({"operations":[raw_op("delete",[doc.units[1].id])]},doc,[doc.units[1].id],())
        rendered=compile_edits(doc,ops)
        text,actions,mapping=finalize_scene_text(rendered.text,("AB",),{"AB":"C"},rendered.source_mapping)
        self.assertEqual(text,"XAAAAAACBBBBBBY")
        self.assertEqual(mapping[1]["output_start"],mapping[1]["output_end"])
        self.assertEqual(mapping[1]["output_lines"],[])

    def test_shorter_scene_delivery_resumes_without_ai_repeat_and_tracks_final_literals(self):
        from dataclasses import replace
        from test_pipeline import PipelineTests
        from novel_monitor import processor
        original = "开头。\n" + "刺激细节。" * 40 + "\n结局离开。"
        doc = parse_document(original, 800)
        ops = parse_edit_ops({"operations": [raw_op("delete", [doc.units[1].id])]}, doc, [doc.units[1].id], ())
        rendered = compile_edits(doc, ops)
        result = SceneReviewResult(rendered.text, "complete", (), 4, 1, (), (), rendered.source_mapping, rendered.edit_actions)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = replace(PipelineTests.rewrite_config(root), semantic_compliance_enabled=True)
            source = config.input_dir / "原书名.txt"
            source.write_text(original, encoding="utf-8")
            (root / "指定违禁词.txt").write_text("离开=告别\n开头", encoding="utf-8")
            with patch.object(processor, "review_and_rewrite", return_value=result) as api, patch.object(processor, "send_file", side_effect=RuntimeError("webhook error")):
                with self.assertRaisesRegex(RuntimeError, "webhook"):
                    processor.process_file(source, config, logging.getLogger("test"))
            claimed = next(processor.rewrite_processing_dir(config).glob("*/原书名.txt"))
            with patch.object(processor, "review_and_rewrite", side_effect=AssertionError("重复AI")), patch.object(processor, "send_file") as send:
                out = processor.process_file(claimed, config, logging.getLogger("test"))
            self.assertEqual(api.call_count, 1)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(out.read_text(encoding="utf-8-sig"), "〔省略〕。\n结局告别。")
            report = json.loads(out.with_suffix(".semantic-review.json").read_text(encoding="utf-8"))
            self.assertEqual(report["version"], 2)
            self.assertEqual(report["source_name"], "原书名.txt")
            self.assertEqual(report["source_mapping"][1]["output_lines"], [])
            self.assertEqual(report["ai_review_text_sha256"], hashlib.sha256(rendered.text.encode()).hexdigest())
            self.assertNotEqual(report["ai_review_text_sha256"], report["final_text_sha256"])
            self.assertEqual(report["final_text_sha256"],hashlib.sha256(out.read_bytes()).hexdigest())
            self.assertEqual(report["output_sha256"],report["final_text_sha256"])
            self.assertEqual({a["kind"] for a in report["postprocess_actions"]}, {"replace", "mask"})


class HarnessTests(unittest.TestCase):
    def test_comparison_cannot_read_holdout_human_text_before_all_books_complete(self):
        from tools.scene_trial_report import build_report
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/"source").mkdir();(root/"auto").mkdir()
            (root/"source/书.txt").write_text("原文。",encoding="utf-8")
            (root/"auto/summary.json").write_text(json.dumps(dict(complete=0,technical_incomplete=1,books=[])),encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"尚未全部完成"):
                build_report(root/"source",root/"auto",root/"human_not_available",root/"split.json",root/"out")
            self.assertFalse((root/"out").exists())

    def test_trial_retries_technical_review_automatically_without_manual_sentence_edits(self):
        from tools.isolated_scene_trial import run_trial
        doc=parse_document("原文。")
        rendered=compile_edits(doc,())
        result=SceneReviewResult(rendered.text,"complete",(),1,0,(),(),rendered.source_mapping,())
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/"source").mkdir();(root/"source/书.txt").write_text("原文。",encoding="utf-8")
            with patch("tools.isolated_scene_trial.review_and_rewrite",side_effect=[SceneProtocolError("temporary invalid JSON"),result]):
                summary=run_trial(root/"source",root/"out","test")
            self.assertEqual(summary["complete"],1)
            self.assertEqual((root/"out/书.txt").read_text(encoding="utf-8"),"原文。")

    def test_trial_uses_original_names_hashes_and_refuses_existing_directory(self):
        from tools.isolated_scene_trial import run_trial
        source = "开篇。\n结局。"
        doc = parse_document(source)
        rendered = compile_edits(doc, ())
        result = SceneReviewResult(source, "complete", (), 1, 0, (), (), rendered.source_mapping, ())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            originals = root / "originals"
            originals.mkdir()
            (originals / "原名.txt").write_text(source,encoding="utf-8")
            split = root / "split.json"
            split.write_text('{}',encoding="utf-8")
            with patch("tools.isolated_scene_trial.review_and_rewrite", return_value=result):
                summary = run_trial(originals, root / "out", "test", split=split, only=["原名.txt"])
                with self.assertRaises(FileExistsError):
                    run_trial(originals, root / "out", "test", split=split)
            self.assertTrue((root/"out/原名.txt").exists())
            self.assertEqual(summary["complete"],1)
            report=json.loads((root/"out/原名-review.json").read_text(encoding="utf-8"))
            self.assertEqual(report["final_text_sha256"],hashlib.sha256((root/"out/原名.txt").read_bytes()).hexdigest())
            self.assertEqual((originals/"原名.txt").read_text(encoding="utf-8"),source)

    def test_trial_technical_failure_cannot_write_success_txt(self):
        from tools.isolated_scene_trial import run_trial
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            originals=root/"originals"; originals.mkdir()
            (originals/"书.txt").write_text("正文。",encoding="utf-8")
            with patch("tools.isolated_scene_trial.review_and_rewrite",side_effect=SceneProtocolError("协议错误")):
                summary=run_trial(originals,root/"out","test")
            self.assertEqual(summary["technical_incomplete"],1)
            self.assertFalse((root/"out/书.txt").exists())


if __name__ == "__main__":
    unittest.main()
