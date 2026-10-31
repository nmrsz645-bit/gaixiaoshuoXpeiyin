import hashlib
import json
import logging
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from novel_monitor import processor
from novel_monitor.scene_document import parse_document, compile_edits
from novel_monitor.scene_review import SceneReviewResult
import test_pipeline as pipeline_tests


class TextPreparationTests(unittest.TestCase):
    def prepare(self,text):
        try:
            from novel_monitor.text_preparation import prepare_text
        except ImportError:
            self.fail('input/final text preparation is not implemented')
        return prepare_text(text)

    def test_exact_numeric_line_and_chapter_and_numbered_body_are_removed(self):
        text='正文甲\r\n 1 \r\n8\n12\n一\n第1章\n第一章 开始\n1.正文也删除\n12.这行也删除\n正文乙\n3.14是圆周率\n2026.10.18\n'
        result=self.prepare(text)
        self.assertEqual(result.text,'正文甲\r\n12\n一\n正文乙\n3.14是圆周率\n2026.10.18\n')
        self.assertEqual(sum(a['kind']=='delete_line' for a in result.actions),6)

    def test_hash_prefixed_chapter_and_single_digit_lines_are_removed_whole(self):
        text='正文甲\r\n ###第1章 1  \r\n###1\n###第1章\n# 第一章 开始\n###### 8\n###1.正文也删除\n正文乙\n'
        result=self.prepare(text)
        self.assertEqual(result.text,'正文甲\r\n正文乙\n')
        self.assertEqual([a['line'] for a in result.actions],[2,3,4,5,6,7])
        self.assertEqual(result.kept_spans[-1]['input_end'],len(text))

    def test_hash_prefixed_body_and_multidigit_and_decimal_lines_are_preserved(self):
        text='###正文标题\n正文里的###第1章不删除\n###12\n###3.14是圆周率\n###2026.10.19\n'
        result=self.prepare(text)
        self.assertEqual(result.text,text)
        self.assertEqual(result.actions,())

    def test_remove_lines_before_counting_7000_nonspace_characters(self):
        result=self.prepare('1\n第1章\n1.删掉这句\n'+'甲， \n'*3501+'尾声')
        self.assertEqual(len(''.join(result.text.split())),7000)
        self.assertTrue(result.text.endswith('甲，'))
        self.assertNotIn('尾声',result.text)
        self.assertEqual(result.actions[-1]['kind'],'truncate')

    def test_exactly_7000_characters_are_preserved_with_whitespace(self):
        text=('甲， \n'*3500)+'\n'
        self.assertEqual(self.prepare(text).text,text)
        self.assertEqual(self.prepare(text).actions,())

    def test_output_mapping_collapses_deleted_lines_and_truncated_tail(self):
        try:from novel_monitor.text_preparation import remap_output_mapping
        except ImportError:self.fail('deletion mapping is not implemented')
        text='正文\n1\n尾声'
        result=self.prepare(text)
        rows=[dict(source_id='a',output_start=0,output_end=3),dict(source_id='b',output_start=3,output_end=5),dict(source_id='c',output_start=5,output_end=7)]
        mapped=remap_output_mapping(rows,result)
        self.assertEqual([(r['output_start'],r['output_end']) for r in mapped],[(0,3),(3,3),(3,5)])
        self.assertEqual(mapped[1]['output_lines'],[])
        self.assertEqual(mapped[2]['output_lines'],[2])

    def test_real_processor_prepares_before_both_ai_paths_and_keeps_raw_backup(self):
        original='###第1章 1\n###1\n###第1章\n第1章\n1\n1.删掉正文\n'+'甲'*7001+'末尾'
        expected='甲'*7000
        for enabled in (False,True):
            with self.subTest(enabled=enabled),tempfile.TemporaryDirectory() as tmp:
                config=replace(pipeline_tests.PipelineTests.rewrite_config(Path(tmp)),semantic_compliance_enabled=enabled)
                path=config.input_dir/'原书名.txt';raw=original.encode('gb18030');path.write_bytes(raw)
                def review(**kw):
                    self.assertEqual(kw['original'],expected)
                    rendered=compile_edits(parse_document(expected,600),())
                    return SceneReviewResult(rendered.text,'complete',(),1,0,(),(),rendered.source_mapping,())
                def legacy(**kw):self.assertEqual(kw['text'],expected);return expected
                with patch.object(processor,'review_and_rewrite',side_effect=review),patch.object(processor,'optimize_text',side_effect=legacy),patch.object(processor,'send_file') as send:
                    out=processor.process_file(path,config,logging.getLogger('test-preparation'))
                self.assertEqual(out.read_text(encoding='utf-8'),expected)
                backups=list((config.root/'原文备份').glob('*/原文/原书名.txt'))
                self.assertEqual(len(backups),1);self.assertEqual(backups[0].read_bytes(),raw)
                self.assertEqual(send.call_count,1)
                report=json.loads(out.with_suffix('.preparation.json').read_text(encoding='utf-8'))
                self.assertEqual(report['prepared_characters'],7000)
                self.assertEqual(report['source_sha256'],hashlib.sha256(raw).hexdigest())

    def test_final_generated_numbered_lines_and_overflow_are_removed_after_ai(self):
        original='甲'*6999+'。'
        candidate='###第1章 1\n###1\n###第1章\n'+original+'\n1\n第1章\n1.新正文\n多余尾巴'
        with tempfile.TemporaryDirectory() as tmp:
            config=pipeline_tests.PipelineTests.rewrite_config(Path(tmp));source=config.input_dir/'书名.txt';source.write_text(original,encoding='utf-8')
            with patch.object(processor,'optimize_text',return_value=candidate),patch.object(processor,'validate_optimized_text'),patch.object(processor,'send_file'):
                out=processor.process_file(source,config,logging.getLogger('test-preparation'))
            self.assertEqual(out.read_text(encoding='utf-8'),original)

    def test_empty_after_cleaning_keeps_source_and_never_calls_ai_or_sends(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=pipeline_tests.PipelineTests.rewrite_config(Path(tmp));source=config.input_dir/'书名.txt';source.write_text('1\n第1章\n1.正文',encoding='utf-8')
            with patch.object(processor,'optimize_text') as ai,patch.object(processor,'send_file') as send:
                with self.assertRaisesRegex(ValueError,'没有正文'):processor.process_file(source,config,logging.getLogger('test-preparation'))
            ai.assert_not_called();send.assert_not_called()
            self.assertTrue(any(processor.rewrite_processing_dir(config).glob('*/书名.txt')))

    def test_old_sent_stage_recovers_without_duplicate_send_or_rewrite(self):
        original='第1章\n正文。\n尾声。'
        for enabled in (False,True):
            with self.subTest(enabled=enabled),tempfile.TemporaryDirectory() as tmp:
                config=replace(pipeline_tests.PipelineTests.rewrite_config(Path(tmp)),semantic_compliance_enabled=enabled)
                path=config.input_dir/'书名.txt';path.write_text(original,encoding='utf-8')
                doc=parse_document('正文。\n尾声。');rendered=compile_edits(doc,())
                result=SceneReviewResult(rendered.text,'complete',(),1,0,(),(),rendered.source_mapping,())
                with patch.object(processor,'review_and_rewrite',return_value=result),patch.object(processor,'optimize_text',return_value=doc.original),patch.object(processor,'send_file'),patch.object(processor,'_publish_staged',side_effect=RuntimeError('pause publish')):
                    with self.assertRaisesRegex(RuntimeError,'pause publish'):processor.process_file(path,config,logging.getLogger('test-preparation'))
                claimed=processor.recover_claimed_sources(config)[0];folder=claimed.parent/'rewrite'
                staged=folder/'staged/书名.txt';staged.write_bytes(original.encode('utf-8'))
                metadata=json.loads((folder/'staged.json').read_text(encoding='utf-8'))
                metadata.pop('text_preparation_revision');metadata.pop('text_preparation');metadata['text_sha256']=hashlib.sha256(staged.read_bytes()).hexdigest()
                if enabled:
                    old_doc=parse_document(original);old=compile_edits(old_doc,())
                    metadata['source_mapping']=list(old.source_mapping);metadata['edit_actions']=[]
                    metadata['ai_review_text_sha256']=metadata['final_text_sha256']=metadata['text_sha256']
                (folder/'staged.json').write_text(json.dumps(metadata),encoding='utf-8')
                signature=hashlib.sha256((metadata['request_signature']+'\0'+metadata['text_sha256']).encode('ascii')).hexdigest()
                next(folder.glob('webhook.*.sent')).write_text(signature,encoding='utf-8')
                with patch.object(processor,'optimize_text',side_effect=AssertionError('duplicate AI')),patch.object(processor,'review_and_rewrite',side_effect=AssertionError('duplicate scene AI')),patch.object(processor,'send_file') as send:
                    out=processor.process_file(claimed,config,logging.getLogger('test-preparation'))
                send.assert_not_called();self.assertEqual(out.read_text(encoding='utf-8'),original)

    def test_old_sent_stage_recovers_when_new_heading_cleanup_removes_all_source(self):
        original='###1\n###2\n###3'
        for enabled in (False,True):
            with self.subTest(enabled=enabled),tempfile.TemporaryDirectory() as tmp:
                config=replace(pipeline_tests.PipelineTests.rewrite_config(Path(tmp)),semantic_compliance_enabled=enabled)
                path=config.input_dir/'书名.txt';path.write_text('已有正文。',encoding='utf-8')
                rendered=compile_edits(parse_document('已有正文。'),())
                result=SceneReviewResult(rendered.text,'complete',(),1,0,(),(),rendered.source_mapping,())
                with patch.object(processor,'review_and_rewrite',return_value=result),patch.object(processor,'optimize_text',return_value='已有正文。'),patch.object(processor,'send_file'),patch.object(processor,'_publish_staged',side_effect=RuntimeError('pause publish')):
                    with self.assertRaisesRegex(RuntimeError,'pause publish'):processor.process_file(path,config,logging.getLogger('test-preparation'))
                claimed=processor.recover_claimed_sources(config)[0];folder=claimed.parent/'rewrite'
                claimed.write_bytes(original.encode('utf-8'))
                staged=folder/'staged/书名.txt';staged.write_bytes(original.encode('utf-8'))
                metadata=json.loads((folder/'staged.json').read_text(encoding='utf-8'))
                metadata.pop('text_preparation_revision');metadata.pop('text_preparation')
                metadata['source_sha256']=metadata['text_sha256']=hashlib.sha256(original.encode('utf-8')).hexdigest()
                if enabled:
                    metadata['source_mapping']=list(compile_edits(parse_document(original),()).source_mapping)
                    metadata['ai_review_text_sha256']=metadata['final_text_sha256']=metadata['text_sha256']
                (folder/'staged.json').write_text(json.dumps(metadata),encoding='utf-8')
                signature=hashlib.sha256((metadata['request_signature']+'\0'+metadata['text_sha256']).encode('ascii')).hexdigest()
                next(folder.glob('webhook.*.sent')).write_text(signature,encoding='utf-8')
                with patch.object(processor,'optimize_text',side_effect=AssertionError('duplicate AI')),patch.object(processor,'review_and_rewrite',side_effect=AssertionError('duplicate scene AI')),patch.object(processor,'send_file') as send:
                    out=processor.process_file(claimed,config,logging.getLogger('test-preparation'))
                send.assert_not_called();self.assertEqual(out.read_text(encoding='utf-8'),original)

    def test_backup_collision_stops_before_ai_and_preserves_both_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=pipeline_tests.PipelineTests.rewrite_config(Path(tmp));source=config.input_dir/'书名.txt';raw='第1章\n正文。'.encode();source.write_bytes(raw)
            folder=config.root/'原文备份'/hashlib.sha256(raw).hexdigest()[:24]/'原文';folder.mkdir(parents=True)
            backup=folder/source.name;backup.write_bytes(b'existing data')
            with patch.object(processor,'optimize_text') as ai,patch.object(processor,'send_file') as send:
                with self.assertRaisesRegex(RuntimeError,'备份'):processor.process_file(source,config,logging.getLogger('test-preparation'))
            ai.assert_not_called();send.assert_not_called();self.assertEqual(backup.read_bytes(),b'existing data')
            self.assertTrue(any(processor.rewrite_processing_dir(config).glob('*/书名.txt')))

    def test_scene_report_keeps_prepared_source_and_final_deletion_mapping_distinct(self):
        from novel_monitor.scene_document import EditOp
        with tempfile.TemporaryDirectory() as tmp:
            config=replace(pipeline_tests.PipelineTests.rewrite_config(Path(tmp)),semantic_compliance_enabled=True)
            source=config.input_dir/'原书名.txt';source.write_text('第1章\n开头。\n结局。',encoding='utf-8')
            doc=parse_document('开头。\n结局。')
            rendered=compile_edits(doc,(EditOp('rewrite',(doc.units[0].id,),('开头。\n1.新正文也删除',),'模型补句',()),))
            result=SceneReviewResult(rendered.text,'complete',(),1,0,(),(),rendered.source_mapping,rendered.edit_actions)
            with patch.object(processor,'review_and_rewrite',return_value=result) as ai,patch.object(processor,'send_file'):
                out=processor.process_file(source,config,logging.getLogger('test-preparation'))
            ai.assert_called_once();final=out.read_bytes()
            self.assertEqual(final.decode('utf-8'),'开头。\n结局。')
            report=json.loads(out.with_suffix('.semantic-review.json').read_text(encoding='utf-8'))
            self.assertEqual(report['source_mapping_basis'],'prepared_source')
            self.assertEqual(Path(report['text_preparation']['prepared_source']).read_text(encoding='utf-8'),doc.original)
            self.assertEqual(report['final_text_sha256'],hashlib.sha256(final).hexdigest())
            self.assertEqual(report['ai_review_text_sha256'],hashlib.sha256(rendered.text.encode()).hexdigest())
            self.assertTrue(all(0<=m['output_start']<=m['output_end']<=len(final.decode('utf-8')) for m in report['source_mapping']))
            self.assertTrue(any(a['kind']=='delete_line' for a in report['postprocess_actions']))


if __name__=='__main__':unittest.main()
