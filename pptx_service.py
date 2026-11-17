"""Local file selection and supervised, shell-free PPTX extraction."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time


class ExtractionCancelled(Exception):
    pass


def load_settings(directory):
    path = Path(directory) / "pptx-settings.json"
    if not path.exists():
        return {"template": "", "output_dir": "", "rules": [], "mapping": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, OSError):
        raise ValueError("PPTX 配置无法读取，请重新保存配置。")


def save_settings(directory, body):
    template = validate_file(body.get("template"), "xlsx")
    rules, mapping = body.get("rules", []), body.get("mapping")
    if not isinstance(rules, list) or len(rules) > 500:
        raise ValueError("字段别名最多 500 条。")
    for rule in rules:
        if (not isinstance(rule, dict) or not isinstance(rule.get("source"), str)
                or not rule["source"].strip() or len(rule["source"]) > 100
                or type(rule.get("target")) is not int or not 0 <= rule["target"] < 19):
            raise ValueError("字段别名格式无效。")
    analysis = invoke({"action":"analyze_template", "template":template, "rules":rules},
                      threading.Event(), lambda event: None, timeout=60)
    if mapping is None:
        mapping = analysis["mapping"]
    if (not isinstance(mapping, list) or len(mapping) != len(analysis["headers"])
            or any(type(field) is not int or not -1 <= field < len(analysis["fields"]) for field in mapping)):
        raise ValueError("模板列匹配无效，请重新分析模板。")
    output = body.get("output_dir", "")
    if not isinstance(output, str) or (output and not Path(output).is_dir()):
        raise ValueError("保存目录不存在。")
    value = {"template":template, "output_dir":output, "rules":rules, "mapping":mapping}
    target = Path(directory) / "pptx-settings.json"
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False)
    try:
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return dict(value, analysis=analysis)


def choose_files(kind):
    if kind not in {"pptx", "xlsx"}:
        raise ValueError("文件类型无效。")
    if sys.platform == "win32":
        script = '''[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.OpenFileDialog
$d.Filter = $env:AIOA_FILE_FILTER
$d.Multiselect = ($env:AIOA_FILE_KIND -eq 'pptx')
try { if ($d.ShowDialog() -eq 'OK') { ConvertTo-Json -Compress -InputObject @($d.FileNames) } else { '[]' } } finally { $d.Dispose() }'''
        command = ["powershell.exe", "-NoProfile", "-STA", "-Command", script]
        env = dict(os.environ, AIOA_FILE_KIND=kind, AIOA_FILE_FILTER=f"{kind.upper()}|*.{kind}")
    elif sys.platform == "darwin":
        script = '''on run argv
try
activate
set selectedFiles to choose file of type {item 1 of argv} with multiple selections allowed
set resultText to ""
repeat with selectedFile in selectedFiles
set resultText to resultText & POSIX path of selectedFile & linefeed
end repeat
return resultText
on error number -128
return ""
end try
end run'''
        command, env = ["osascript", "-e", script, kind], None
    else:
        raise ValueError("本地文件选择目前支持 Windows 和 macOS。")
    result = subprocess.run(command, capture_output=True, encoding="utf-8", env=env)
    if result.returncode:
        raise ValueError("无法打开文件选择窗口。")
    paths = json.loads(result.stdout or "[]") if sys.platform == "win32" else result.stdout.strip().splitlines()
    if kind == "xlsx" and len(paths) > 1:
        raise ValueError("请只选择一个 XLSX 模板。")
    return [validate_file(path, kind) for path in paths]


def validate_file(value, extension):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("文件路径无效。")
    path = Path(value).expanduser().resolve()
    if path.suffix.lower() != f".{extension}" or not path.is_file():
        raise ValueError(f"请选择可读取的 {extension.upper()} 文件。")
    with path.open("rb"):
        pass
    return str(path)


def prepare_request(body):
    inputs = body.get("inputs")
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= 1000:
        raise ValueError("请选择 1–1000 个 PPTX 文件。")
    result = {"action": "extract", "inputs": list(dict.fromkeys(validate_file(p, "pptx") for p in inputs)),
              "template": validate_file(body.get("template"), "xlsx"), "rules": body.get("rules", [])}
    if "mapping" in body:
        result["mapping"] = body["mapping"]
    if body.get("output_dir"):
        directory = Path(body["output_dir"]).expanduser().resolve()
        if not directory.is_dir():
            raise ValueError("保存目录不存在。")
        result["output_dir"] = str(directory)
    # A detached JSON snapshot prevents UI changes from affecting a running task.
    return json.loads(json.dumps(result, ensure_ascii=False))


def executable():
    name = "pptx-extractor.exe" if sys.platform == "win32" else "pptx-extractor"
    binary = Path(__file__).resolve().parent / "pptx_extractor" / "bin" / name
    if not binary.is_file():
        raise ValueError("PPTX 提取组件尚未构建，请先构建本机提取内核。")
    return binary


def invoke(request, cancel, on_event, *, binary=None, timeout=3600):
    if request.get("action") == "extract":
        destination = Path(request.get("output_dir") or Path(request["inputs"][0]).parent)
        with tempfile.TemporaryDirectory(prefix=".pptx-", dir=destination) as staged:
            result = _invoke(dict(request, output_dir=staged), cancel,
                             lambda event: on_event(event) if event.get("type") != "done" else None,
                             binary=binary, timeout=timeout)
            if cancel.is_set():
                raise ExtractionCancelled()
            source = Path(result["output"])
            if source.parent.resolve() != Path(staged).resolve() or not source.is_file():
                raise ValueError("提取组件返回的输出文件无效。")
            for index in range(10000):
                name = "面辅料汇总" + (f" ({index})" if index else "") + ".xlsx"
                output = destination / name
                try:
                    os.link(source, output)
                    break
                except FileExistsError:
                    continue
            else:
                raise ValueError("同名结果文件过多，请整理保存目录。")
            result = dict(result, output=str(output))
            on_event(result)
            return result
    return _invoke(request, cancel, on_event, binary=binary, timeout=timeout)


def _invoke(request, cancel, on_event, *, binary=None, timeout=3600):
    binary = binary or executable()
    events = queue.Queue()
    with tempfile.TemporaryFile() as diagnostics:
        process = subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=diagnostics, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        def read_events():
            try:
                for line in process.stdout:
                    events.put(line)
            finally:
                events.put(None)
        reader = threading.Thread(target=read_events, daemon=True)
        reader.start()
        final = None
        started = time.monotonic()
        try:
            process.stdin.write(json.dumps(request, ensure_ascii=False).encode("utf-8"))
            process.stdin.close()
            while True:
                if cancel.is_set():
                    raise ExtractionCancelled()
                if time.monotonic() - started > timeout:
                    raise ValueError("提取超时，已停止组件，请减少文件后重试。")
                try:
                    line = events.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    break
                event = json.loads(line)
                if event.get("type") == "error":
                    raise ValueError(event.get("error", "提取失败。"))
                if event.get("type") in {"done", "template"}:
                    final = event
                on_event(event)
            code = process.wait(timeout=5)
            if code or final is None:
                raise ValueError("提取组件意外退出，未生成有效结果。")
            return final
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdout.close()
            if not process.stdin.closed:
                process.stdin.close()
            reader.join(timeout=2)
