"""Local file selection and supervised, shell-free PPTX extraction."""
import json
import errno
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time

MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_EVENT_BYTES = 1024 * 1024


def validate_options(value):
    if not isinstance(value, dict):
        raise ValueError("提取配置格式无效。")
    rules = value.get("rules", [])
    if not isinstance(rules, list) or len(rules) > 500:
        raise ValueError("字段别名最多 500 条。")
    seen = set()
    for rule in rules:
        if (not isinstance(rule, dict) or not isinstance(rule.get("source"), str)
                or not rule["source"].strip() or len(rule["source"]) > 100
                or type(rule.get("target")) is not int or not 0 <= rule["target"] < 19):
            raise ValueError("字段别名格式无效。")
        key = "".join(rule["source"].split()).upper()
        if key in seen:
            raise ValueError("字段别名重复或冲突，请每个原字段只保留一条。")
        seen.add(key)
    mapping = value.get("mapping")
    if mapping is not None and (not isinstance(mapping, list) or len(mapping) > 16384
            or any(type(field) is not int or not -1 <= field < 19 for field in mapping)):
        raise ValueError("模板列匹配无效，请重新分析模板。")
    if not isinstance(value.get("output_dir", ""), str):
        raise ValueError("保存目录格式无效。")


class ExtractionCancelled(Exception):
    pass


def load_settings(directory):
    path = Path(directory) / "pptx-settings.json"
    if not path.exists():
        return {"template": "", "output_dir": "", "rules": [], "mapping": []}
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_REQUEST_BYTES + 1)
        if len(content) > MAX_REQUEST_BYTES:
            raise ValueError()
        value = json.loads(content)
        validate_options(value)
        if not isinstance(value.get("template", ""), str):
            raise ValueError()
        return {"template":value.get("template", ""), "output_dir":value.get("output_dir", ""),
                "rules":value.get("rules", []), "mapping":value.get("mapping") or []}
    except (ValueError, OSError):
        raise ValueError("PPTX 配置无法读取，请重新保存配置。")


def save_settings(directory, body):
    validate_options(body)
    template = validate_file(body.get("template"), "xlsx")
    rules, mapping = body.get("rules", []), body.get("mapping")
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


def validate_file(value, extension, *, must_exist=True):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("文件路径无效。")
    try:
        path = Path(value).expanduser().resolve()
        if path.suffix.lower() != f".{extension}" or (must_exist and not path.is_file()):
            raise ValueError(f"请选择可读取的 {extension.upper()} 文件。")
        if must_exist:
            with path.open("rb"):
                pass
    except (OSError, RuntimeError) as error:
        raise ValueError(f"无法读取 {extension.upper()} 文件，请检查文件是否存在及访问权限。") from error
    return str(path)


def prepare_request(body):
    validate_options(body)
    inputs = body.get("inputs")
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= 1000:
        raise ValueError("请选择 1–1000 个 PPTX 文件。")
    result = {"action": "extract", "inputs": list(dict.fromkeys(validate_file(p, "pptx", must_exist=False) for p in inputs)),
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
    if cancel.is_set():
        raise ExtractionCancelled()
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
                if cancel.is_set():
                    raise ExtractionCancelled()
                name = "面辅料汇总" + (f" ({index})" if index else "") + ".xlsx"
                output = destination / name
                try:
                    os.link(source, output)
                    break
                except FileExistsError:
                    continue
                except OSError as error:
                    if error.errno not in {errno.EPERM, errno.EACCES, errno.EXDEV, errno.ENOTSUP, errno.EINVAL}:
                        raise
                    # FAT/exFAT and some shares do not support hard links.
                    # Reserve the new name exclusively and remove it on failure.
                    try:
                        target = output.open("xb")
                    except FileExistsError:
                        continue
                    try:
                        with target, source.open("rb") as stream:
                            while chunk := stream.read(1024 * 1024):
                                if cancel.is_set():
                                    raise ExtractionCancelled()
                                target.write(chunk)
                            if cancel.is_set():
                                raise ExtractionCancelled()
                            target.flush()
                            os.fsync(target.fileno())
                    except BaseException:
                        output.unlink(missing_ok=True)
                        raise
                    break
            else:
                raise ValueError("同名结果文件过多，请整理保存目录。")
            result = dict(result, output=str(output))
            on_event(result)
            return result
    return _invoke(request, cancel, on_event, binary=binary, timeout=timeout)


def _invoke(request, cancel, on_event, *, binary=None, timeout=3600):
    if cancel.is_set():
        raise ExtractionCancelled()
    payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_REQUEST_BYTES:
        raise ValueError("提取参数过多，请减少文件或字段规则后重试。")
    binary = binary or executable()
    events = queue.Queue(maxsize=128)
    stopping = threading.Event()
    def enqueue(value):
        while not stopping.is_set():
            try:
                events.put(value, timeout=0.1)
                return
            except queue.Full:
                pass
    with tempfile.TemporaryFile() as diagnostics:
        process = subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=diagnostics, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        def read_events():
            try:
                while line := process.stdout.readline(MAX_EVENT_BYTES + 1):
                    if len(line) > MAX_EVENT_BYTES:
                        enqueue(ValueError("提取组件返回的数据过大，已停止任务。"))
                        return
                    enqueue(line)
                    if stopping.is_set():
                        return
            except OSError:
                enqueue(ValueError("无法读取提取组件返回结果。"))
            finally:
                enqueue(None)
        def write_request():
            try:
                process.stdin.write(payload)
                process.stdin.close()
            except (OSError, ValueError):
                enqueue(ValueError("无法发送提取参数，组件可能已退出。"))
        reader = threading.Thread(target=read_events, daemon=True)
        writer = threading.Thread(target=write_request, daemon=True)
        reader.start()
        writer.start()
        final = None
        read_done = False
        expected = "done" if request.get("action") == "extract" else "template"
        started = time.monotonic()
        try:
            while True:
                if cancel.is_set():
                    raise ExtractionCancelled()
                if time.monotonic() - started > timeout:
                    raise ValueError("提取超时，已停止组件，请减少文件后重试。")
                if read_done and process.poll() is not None:
                    break
                try:
                    line = events.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    read_done = True
                    continue
                if isinstance(line, Exception):
                    raise line
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError) as error:
                    raise ValueError("提取组件返回格式异常，请重新启动工作台。") from error
                if (not isinstance(event, dict) or not isinstance(event.get("type"), str)
                        or event["type"] not in {"progress", "file", "done", "template", "error"}):
                    raise ValueError("提取组件返回了无效事件。")
                if event.get("type") == "error":
                    raise ValueError(event.get("error", "提取失败。"))
                if event.get("type") in {"done", "template"}:
                    if event["type"] != expected or final is not None:
                        raise ValueError("提取组件返回的结果类型异常。")
                    if expected == "done" and (not isinstance(event.get("output"), str)
                            or any(type(event.get(key)) is not int or event[key] < 0
                                   for key in ("success", "failed", "count"))):
                        raise ValueError("提取组件返回的汇总结果无效。")
                    if expected == "template" and (not isinstance(event.get("headers"), list)
                            or not isinstance(event.get("fields"), list)
                            or not isinstance(event.get("mapping"), list)
                            or len(event["headers"]) != len(event["mapping"])
                            or not all(isinstance(value, str) for value in event["headers"] + event["fields"])
                            or any(type(field) is not int or not -1 <= field < len(event["fields"])
                                   for field in event["mapping"])):
                        raise ValueError("提取组件返回的模板信息无效。")
                    final = event
                on_event(event)
            code = process.returncode
            if code or final is None:
                raise ValueError("提取组件意外退出，未生成有效结果。")
            return final
        finally:
            stopping.set()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            reader.join(timeout=2)
            writer.join(timeout=2)
            process.stdout.close()
            if not process.stdin.closed:
                process.stdin.close()
