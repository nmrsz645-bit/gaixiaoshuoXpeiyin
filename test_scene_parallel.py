"""Concurrency barriers and unchanged review coverage, without network calls."""
import collections
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest
from unittest.mock import patch

from novel_monitor.scene_model import SceneModelClient
from novel_monitor.scene_document import EditOp, compile_edits, parse_document
from novel_monitor.scene_review import review_scene_candidate
from novel_monitor.scene_review import _edit_groups
from novel_monitor.semantic_compliance import SemanticProtocolError


def client():
    return SceneModelClient(api_key='test', model='test', url='https://test.invalid', timeout=1)


class ParallelReviewTests(unittest.TestCase):
    def test_oversized_atomic_edit_reuses_identical_reason_scope_without_losing_reasons(self):
        doc=parse_document(('重复文字。'*3+'\n')*57)
        ids=[u.id for u in doc.units]
        existing=(EditOp('rewrite',tuple(ids),('压缩后。'*180,),'先前完整有效编辑',()),)
        scenes=[dict(source_ids=ids,reason='风险原因'+str(i)+'，必须保留完整原因。') for i in range(5)]
        c=client();c.max_chars=8000;c.extra_rules='补充规则。'*65;seen=[]
        def request(**kw):
            p=kw['payload'];seen.append(p)
            self.assertLessEqual(len(json.dumps(p,ensure_ascii=False)),8000)
            table=p.get('reason_source_groups',{})
            self.assertEqual(len(p['reasons']),5)
            for original,reason in zip(scenes,p['reasons']):
                resolved=reason.get('source_ids',table.get(reason.get('source_group')))
                self.assertEqual(resolved,original['source_ids'])
                self.assertEqual(reason['reason'],original['reason'])
            self.assertEqual(p['atomic_current_source_groups'],[ids])
            return dict(operations=[],checked_source_count=len(ids),checked_scope=p['scope_id'])
        with patch('novel_monitor.scene_model._request_json',side_effect=request):
            result=_edit_groups(doc,existing,scenes,(),c,8000)
        self.assertEqual(result,existing)
        self.assertEqual(len(seen),1,'one indivisible edit should not be split or repeatedly retried')

    def test_three_checks_overlap_and_results_keep_input_order(self):
        c=client()
        self.assertTrue(callable(getattr(c,'run_checks',None)), 'independent checks need a bounded runner')
        gate=threading.Event();entered=threading.Event();lock=threading.Lock();active=0;peak=0
        def task(i):
            nonlocal active,peak
            with lock:
                active+=1;peak=max(peak,active)
                if active==3:entered.set()
            try:
                if not gate.wait(3):raise TimeoutError('three checks did not overlap')
                return [i]
            finally:
                with lock:active-=1
        with ThreadPoolExecutor(max_workers=1) as outer:
            future=outer.submit(c.run_checks,[lambda i=i:task(i) for i in range(7)])
            try:
                self.assertTrue(entered.wait(2),'checks are still serial')
                self.assertFalse(future.done(),'delivery must wait for outstanding checks')
            finally:gate.set()
            self.assertEqual(future.result(3),[[i] for i in range(7)])
        self.assertEqual(peak,3)

    def test_failed_check_waits_for_other_checks_and_raises(self):
        c=client()
        self.assertTrue(callable(getattr(c,'run_checks',None)))
        gate=threading.Event();started=threading.Event();failed=threading.Event();finished=threading.Event()
        def slow():
            started.set()
            if not gate.wait(3):raise TimeoutError('slow check never released')
            finished.set();return []
        def bad():
            if not started.wait(2):raise TimeoutError('slow check never started')
            failed.set();raise SemanticProtocolError('mandatory review incomplete')
        with ThreadPoolExecutor(max_workers=1) as outer:
            future=outer.submit(c.run_checks,[bad,slow])
            try:
                self.assertTrue(failed.wait(2))
                self.assertFalse(future.done(),'failure must drain running checks before retry')
            finally:gate.set()
            with self.assertRaisesRegex(SemanticProtocolError,'mandatory review incomplete'):
                future.result(3)
        self.assertTrue(finished.is_set())

    def test_failure_cancels_not_yet_started_checks(self):
        c=client();gate=threading.Event();blocking=threading.Event();cancelled_work=[]
        def slow():
            blocking.set()
            if not gate.wait(3):raise TimeoutError('blocked review not released')
            return []
        def bad():
            if not blocking.wait(2):raise TimeoutError('review not started')
            raise SemanticProtocolError('failed check')
        # Two occupied workers leave the queued tasks pending until the error
        # has been observed. They must not launch further paid requests.
        c.review_workers=2
        with ThreadPoolExecutor(max_workers=1) as outer:
            future=outer.submit(c.run_checks,[bad,slow]+[lambda:cancelled_work.append(1) for _ in range(20)])
            try:
                self.assertTrue(blocking.wait(2))
                # The executor's live threads are intentionally drained first.
                self.assertFalse(future.done())
            finally:gate.set()
            with self.assertRaises(SemanticProtocolError):future.result(3)
        self.assertEqual(cancelled_work,[],'queued review should be cancelled after a mandatory failure')

    def test_two_books_share_four_request_limit_and_exact_counters(self):
        first,second=client(),client()
        self.assertTrue(callable(getattr(first,'run_checks',None)))
        gate=threading.Event();four=threading.Event();lock=threading.Lock();active=0;peak=0
        def request(**kw):
            nonlocal active,peak
            with lock:
                active+=1;peak=max(peak,active)
                if active>=4:four.set()
            try:
                if not gate.wait(3):raise TimeoutError('four requests did not overlap')
                return {'value':kw['payload']['i']}
            finally:
                with lock:active-=1
        def run(c):
            return c.run_checks([lambda i=i:c.ask('audit_content','prompt',{'i':i},lambda v:v['value']) for i in range(6)])
        with patch('novel_monitor.scene_model._request_json',side_effect=request),ThreadPoolExecutor(max_workers=2) as outer:
            futures=[outer.submit(run,c) for c in (first,second)]
            try:self.assertTrue(four.wait(2))
            finally:gate.set()
            self.assertEqual([f.result(3) for f in futures],[list(range(6)),list(range(6))])
        self.assertEqual(peak,4)
        self.assertEqual((first.audit_calls,second.audit_calls),(6,6))

    def test_parallel_and_serial_keep_every_identical_review_request(self):
        doc=parse_document(('普通叙述。'*24+'\n')*18+'结局离开。')
        rendered=compile_edits(doc,(EditOp('rewrite',(doc.units[0].id,),('简化后的普通叙述。',),'删细节',()),))
        ledgers=[]
        for workers in (1,3):
            seen=[];lock=threading.Lock();c=client();c.max_chars=4000;c.extra_rules='';c.review_workers=workers
            def request(**kw):
                p=kw['payload']
                with lock:seen.append(json.dumps([kw['prompt'],p],ensure_ascii=False,sort_keys=True))
                if p['task']=='audit_additions':
                    return dict(additions=[],checked_output_count=p['output_count'],checked_candidate_sha256=p['candidate_sha256'])
                return dict({'issues' if p['task']=='audit_coherence' else 'risks':[]},
                            checked_output_ids=[u['id'] for u in p['output']])
            with patch('novel_monitor.scene_model._request_json',side_effect=request):
                self.assertEqual(review_scene_candidate(doc,rendered,(),c),())
            ledgers.append(collections.Counter(seen))
        self.assertGreater(sum(ledgers[0].values()),6)
        self.assertEqual(ledgers[0],ledgers[1],'speed must not omit or alter any check payload')

    def test_candidate_review_actually_runs_windows_concurrently(self):
        c=client();c.max_chars=4000;c.extra_rules=''
        doc=parse_document(('普通叙述。'*24+'\n')*18+'结局离开。')
        rendered=compile_edits(doc,(EditOp('rewrite',(doc.units[0].id,),('简化后的普通叙述。',),'删细节',()),))
        gate=threading.Event();overlap=threading.Event();lock=threading.Lock();active=0
        def request(**kw):
            nonlocal active
            p=kw['payload']
            with lock:
                active+=1
                if active>=2:overlap.set()
            try:
                if not gate.wait(3):raise TimeoutError('candidate windows still serial')
                if p['task']=='audit_additions':
                    return dict(additions=[],checked_output_count=p['output_count'],checked_candidate_sha256=p['candidate_sha256'])
                return dict({'issues' if p['task']=='audit_coherence' else 'risks':[]},checked_output_ids=[u['id'] for u in p['output']])
            finally:
                with lock:active-=1
        with patch('novel_monitor.scene_model._request_json',side_effect=request),ThreadPoolExecutor(max_workers=1) as outer:
            future=outer.submit(review_scene_candidate,doc,rendered,(),c)
            try:self.assertTrue(overlap.wait(2),'the real candidate path is still serial')
            finally:gate.set()
            self.assertEqual(future.result(3),())


if __name__=='__main__':unittest.main()
