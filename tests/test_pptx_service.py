from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from pptx_service import ExtractionCancelled, invoke, prepare_request, load_settings, save_settings


class PptxServiceTests(unittest.TestCase):
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
