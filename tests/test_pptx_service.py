from pathlib import Path
import errno
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pptx_service import ExtractionCancelled, invoke, prepare_request, load_settings, save_settings, choose_files, validate_file


class PptxServiceTests(unittest.TestCase):
    def test_windows_picker_all_files_filter_and_single_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'中文.PPTX';path.touch()
            completed=subprocess.CompletedProcess([],0,json.dumps([str(path)]),'')
            with patch('pptx_service.sys.platform','win32'),patch('pptx_service.subprocess.run',return_value=completed) as run:
                self.assertEqual(choose_files('pptx',multiple=False),[str(path.resolve())])
                env=run.call_args.kwargs['env']
                self.assertIn('所有文件 (*.*)|*.*',env['AIOA_FILE_FILTER'])
                self.assertIn('*.pptx',env['AIOA_FILE_FILTER'])
                self.assertEqual(env['AIOA_PICKER_MULTIPLE'],'false')
                choose_files('pptx')
                self.assertEqual(run.call_args.kwargs['env']['AIOA_PICKER_MULTIPLE'],'true')
                completed.stdout='[]'
                self.assertEqual(choose_files('pptx',multiple=False),[])

    def test_mac_picker_does_not_hide_pptx_based_on_system_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'微信文件.pptx';path.touch()
            completed=subprocess.CompletedProcess([],0,str(path)+'\n','')
            with patch('pptx_service.sys.platform','darwin'),patch('pptx_service.subprocess.run',return_value=completed) as run:
                self.assertEqual(choose_files('pptx',multiple=False),[str(path.resolve())])
                command=run.call_args.args[0]
                self.assertNotIn('choose file of type',command[2])
                self.assertEqual(command[-1],'single')

    def test_unsupported_format_explains_actual_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            for suffix in ('.ppt','.pptm','.xls'):
                path=Path(tmp)/('wrong'+suffix);path.touch()
                with self.assertRaises(ValueError) as error:
                    validate_file(str(path),'xlsx' if suffix=='.xls' else 'pptx')
                self.assertIn(suffix,str(error.exception))
                self.assertIn('另存为',str(error.exception))
                self.assertIn('不能只修改',str(error.exception))

    def test_corrupt_configuration_and_invalid_options_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'pptx-settings.json'
            for value in ({'rules':None}, {'mapping':[True]}, {'template':[]},
                          {'rules':[{'source':'a','target':0},{'source':' A ','target':1}]}):
                with self.subTest(value=value):
                    path.write_text(json.dumps(value), encoding='utf-8')
                    with self.assertRaisesRegex(ValueError, '配置无法读取'):
                        load_settings(tmp)
            with self.assertRaises(ValueError):
                prepare_request([])

    def test_blocked_stdin_and_closed_stdout_remain_cancellable(self):
        scripts=['import time; time.sleep(30)',
                 'import json,sys,os,time; json.load(sys.stdin); os.close(1); time.sleep(30)']
        for script in scripts:
            with self.subTest(script=script):
                mock,processes=self.fake_component(script)
                cancel=threading.Event();timer=threading.Timer(0.3,cancel.set)
                started=time.monotonic();timer.start()
                try:
                    with mock,self.assertRaises(ExtractionCancelled):
                        invoke({'action':'analyze_template','padding':'x'*1024*1024}, cancel,
                               lambda event:None,binary='test',timeout=5)
                finally:
                    timer.cancel()
                self.assertLess(time.monotonic()-started,5)
                self.assertIsNotNone(processes[0].poll())

    def test_invalid_and_oversized_component_events_fail_cleanly(self):
        for output in ('[]', '{"type":[]}', '{"type":"done","output":"x"}',
                       '{"type":"template","headers":["a"],"fields":["a"],"mapping":[true]}',
                       'not json', 'x'*(1024*1024+1)):
            with self.subTest(length=len(output)):
                # Transfer fixture text through a file to avoid Windows command length limits.
                with tempfile.TemporaryDirectory() as tmp:
                    fixture=Path(tmp)/'event.txt';fixture.write_text(output,encoding='utf-8')
                    script=f'import json,sys,pathlib; json.load(sys.stdin); print(pathlib.Path({str(fixture)!r}).read_text(encoding="utf-8"))'
                    mock,processes=self.fake_component(script)
                    with mock,self.assertRaises(ValueError):
                        invoke({'action':'analyze_template'},threading.Event(),lambda event:None,binary='test')
                    self.assertIsNotNone(processes[0].poll())

    def test_copy_fallback_handles_unsupported_links_and_disk_failure(self):
        script='''import json,sys,pathlib
r=json.load(sys.stdin); p=pathlib.Path(r["output_dir"])/"out.xlsx";p.write_bytes(b"complete workbook")
print(json.dumps({"type":"done","output":str(p),"success":1,"failed":0,"count":2}))
'''
        for disk_failure in (False,True):
            with self.subTest(disk_failure=disk_failure),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);old=root/'面辅料汇总.xlsx';old.write_bytes(b'old')
                mock,processes=self.fake_component(script)
                with mock,patch('pptx_service.os.link',side_effect=OSError(errno.ENOTSUP,'unsupported')):
                    if disk_failure:
                        with patch('pptx_service.os.fsync',side_effect=OSError(errno.ENOSPC,'disk full')):
                            with self.assertRaises(OSError):
                                invoke({'action':'extract','inputs':['a.pptx'],'output_dir':tmp},threading.Event(),lambda event:None,binary='test')
                        self.assertEqual(list(root.iterdir()),[old])
                    else:
                        result=invoke({'action':'extract','inputs':['a.pptx'],'output_dir':tmp},threading.Event(),lambda event:None,binary='test')
                        self.assertEqual(Path(result['output']).read_bytes(),b'complete workbook')
                self.assertEqual(old.read_bytes(),b'old')
                self.assertFalse(list(root.glob('.pptx-*')))

    def test_missing_batch_source_is_left_for_per_file_error_reporting(self):
        with tempfile.TemporaryDirectory() as tmp:
            template=Path(tmp)/'t.xlsx';template.touch()
            missing=Path(tmp)/'deleted.pptx'
            request=prepare_request({'template':str(template),'inputs':[str(missing)]})
            self.assertEqual(request['inputs'],[str(missing.resolve())])

    def test_settings_persist_and_invalid_update_keeps_previous(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); template=root/'t.xlsx'; template.touch()
            self.assertEqual(load_settings(root)['rules'], [])
            analysis={'headers':['款号'], 'mapping':[0], 'fields':['款号']*19}
            with patch('pptx_service.invoke', return_value=analysis):
                body={'template':str(template), 'rules':[{'source':'样式','target':0}], 'mapping':[0], 'output_dir':tmp}
                save_settings(root, body)
                previous=load_settings(root)
                for invalid in (dict(body,mapping=[99]),dict(body,rules=[{'source':'','target':0}])):
                    with self.assertRaises(ValueError):
                        save_settings(root, invalid)
                    self.assertEqual(load_settings(root), previous)

    def fake_component(self, script):
        original = subprocess.Popen
        processes = []
        def launch(command, **kwargs):
            process = original([sys.executable, "-c", script], **kwargs)
            processes.append(process)
            return process
        return patch("pptx_service.subprocess.Popen", side_effect=launch), processes

    def test_atomic_output_no_overwrite_and_event_paths(self):
        script = '''import json,sys,pathlib
r=json.load(sys.stdin)
p=pathlib.Path(r["output_dir"])/"面辅料汇总.xlsx"
p.write_bytes(b"complete workbook")
print(json.dumps({"type":"done","output":str(p),"success":1,"failed":0,"count":2}))
'''
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            previous = directory / "面辅料汇总.xlsx"
            previous.write_bytes(b"old")
            mock, processes = self.fake_component(script)
            events = []
            with mock:
                result = invoke({"action":"extract", "inputs":["input.pptx"], "output_dir":tmp},
                                threading.Event(), events.append, binary="test")
            self.assertEqual(previous.read_bytes(), b"old")
            self.assertEqual(Path(result["output"]).read_bytes(), b"complete workbook")
            self.assertEqual(events[-1]["output"], result["output"])
            self.assertFalse(list(directory.glob(".pptx-*")))
            self.assertIsNotNone(processes[0].poll())

    def test_cancel_and_timeout_reap_process_and_remove_partial_files(self):
        script = '''import json,sys,pathlib,time
r=json.load(sys.stdin)
(pathlib.Path(r["output_dir"])/"partial.xlsx").write_bytes(b"partial")
time.sleep(30)
'''
        for cancel_task in (True, False):
            with self.subTest(cancel=cancel_task), tempfile.TemporaryDirectory() as tmp:
                cancel = threading.Event()
                timer = threading.Timer(0.3, cancel.set)
                if cancel_task:
                    timer.start()
                mock, processes = self.fake_component(script)
                try:
                    with mock, self.assertRaises(ExtractionCancelled if cancel_task else ValueError):
                        invoke({"action":"extract", "inputs":["input.pptx"], "output_dir":tmp},
                               cancel, lambda event: None, binary="test", timeout=1)
                finally:
                    timer.cancel()
                self.assertIsNotNone(processes[0].poll())
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_input_validation_and_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, template = Path(tmp)/"a.pptx", Path(tmp)/"t.xlsx"
            source.touch(); template.touch()
            body = {"inputs":[str(source), str(source)], "template":str(template), "rules":[]}
            request = prepare_request(body)
            body["rules"].append({"source":"a", "target":2})
            self.assertEqual(request["rules"], [])
            self.assertEqual(len(request["inputs"]), 1)
            with self.assertRaises(ValueError):
                prepare_request(dict(body, inputs=[str(template)]))


if __name__ == "__main__":
    unittest.main()
