"""Signed, immutable offline releases; callers must stop tasks before activation."""
from contextlib import contextmanager
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import uuid
from zipfile import BadZipFile, ZipFile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PRODUCT = "office-auto-flow"
BASE_API = 1
WEB_FILES = {"web/" + name for name in (
    "index.html", "workbench.css", "bootstrap.js", "workbench.js", "pptx.js")}
ALLOWED_FILES = WEB_FILES | {"pptx_extractor/bin/pptx-extractor.exe"}
MAX_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024


class UpdateError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UpdateError("更新数据包含重复字段。")
        result[key] = value
    return result


def _json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_object)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise UpdateError("更新数据格式无效。") from error


def verify_manifest(raw, public_keys):
    if len(raw) > MAX_MANIFEST_BYTES:
        raise UpdateError("更新清单过大。")
    envelope = _json(raw)
    try:
        manifest = envelope["manifest"]
        key = public_keys[envelope["key_id"]]
        signature = base64.b64decode(envelope["signature"], validate=True)
        Ed25519PublicKey.from_public_bytes(key).verify(signature, canonical(manifest))
    except (KeyError, TypeError, ValueError, InvalidSignature, RecursionError) as error:
        raise UpdateError("更新包签名无效或发布者不受信任。") from error
    if not isinstance(manifest, dict) or manifest.get("product") != PRODUCT:
        raise UpdateError("更新包不属于本应用。")
    if type(manifest.get("base_api")) is not int or manifest["base_api"] != BASE_API:
        raise UpdateError("更新包需要不同的基础程序，请安装匹配的完整安装包。")
    if manifest.get("format") != 1 or type(manifest["format"]) is not int:
        raise UpdateError("更新包格式版本不受支持。")
    if type(manifest.get("sequence")) is not int or manifest["sequence"] < 1:
        raise UpdateError("更新版本序号无效。")
    if not isinstance(manifest.get("version"), str) or not re.fullmatch(r"[\w.-]{1,64}", manifest["version"]):
        raise UpdateError("更新版本名称无效。")
    files = manifest.get("files")
    if not isinstance(files, dict) or not WEB_FILES <= files.keys() or not files.keys() <= ALLOWED_FILES:
        raise UpdateError("更新文件列表不完整或包含禁止更新的路径。")
    total = 0
    for metadata in files.values():
        if (not isinstance(metadata, dict) or type(metadata.get("size")) is not int
                or not 0 < metadata["size"] <= MAX_BYTES
                or not isinstance(metadata.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"])):
            raise UpdateError("更新文件大小或校验值无效。")
        total += metadata["size"]
    if total > MAX_BYTES:
        raise UpdateError("更新包解压后过大。")
    return manifest


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".update-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class UpdateStore:
    def __init__(self, directory, public_keys):
        self.root = Path(directory)
        self.keys = dict(public_keys)

    @contextmanager
    def _lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.root / "update.lock", os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "r+b") as stream:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise UpdateError("另一个更新操作正在进行，请稍后重试。") from error
            try:
                yield
            finally:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def state(self):
        path = self.root / "state.json"
        if not path.exists():
            return {"active": None, "previous": None, "highest_sequence": 0}
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise UpdateError("更新状态损坏。")
        value = _json(path.read_bytes())
        if (not isinstance(value, dict) or type(value.get("highest_sequence")) is not int
                or value["highest_sequence"] < 0
                or any(value.get(key) is not None and
                       (not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{32}", value[key]))
                       for key in ("active", "previous"))):
            raise UpdateError("更新状态损坏。")
        return value

    def release(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise UpdateError("更新版本标识无效。")
        root = self.root / "releases" / identifier
        if root.is_symlink() or not root.is_dir():
            raise UpdateError("更新版本目录缺失或无效。")
        manifest_path = root / "update.json"
        if manifest_path.is_symlink() or manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            raise UpdateError("更新清单无效。")
        manifest = verify_manifest(manifest_path.read_bytes(), self.keys)
        for name, metadata in manifest["files"].items():
            path = root / name
            relative_parts = [root.joinpath(*Path(name).parts[:index])
                              for index in range(1, len(Path(name).parts) + 1)]
            if (any(part.is_symlink() for part in relative_parts)
                    or not path.is_file() or path.stat().st_size != metadata["size"]
                    or hashlib.sha256(path.read_bytes()).hexdigest() != metadata["sha256"]):
                raise UpdateError("已安装的更新文件校验失败。")
        return root, manifest

    def install(self, package, healthcheck, *, minimum_sequence=0):
        """Validate/stage a full resource snapshot, then atomically select it."""
        if type(minimum_sequence) is not int or minimum_sequence < 0:
            raise UpdateError("基础版本序号无效。")
        with self._lock():
            state = self.state()
            releases = self.root / "releases"
            releases.mkdir(exist_ok=True)
            identifier = uuid.uuid4().hex
            target = releases / identifier
            try:
                package = Path(package)
                if package.stat().st_size > MAX_BYTES:
                    raise UpdateError("更新包过大。")
                with ZipFile(package) as archive:
                    entries = archive.infolist()
                    names = [entry.filename for entry in entries]
                    if len(entries) > len(ALLOWED_FILES) + 1 or len(names) != len(set(names)):
                        raise UpdateError("更新包包含重复或过多文件。")
                    info = archive.getinfo("update.json")
                    if info.file_size > MAX_MANIFEST_BYTES:
                        raise UpdateError("更新清单过大。")
                    raw = archive.read(info)
                    manifest = verify_manifest(raw, self.keys)
                    threshold = max(state["highest_sequence"], minimum_sequence)
                    if manifest["sequence"] <= threshold:
                        raise UpdateError(f"更新包版本不高于已安装版本（最低序号：{threshold}）。")
                    expected = {"update.json"} | {"payload/" + name for name in manifest["files"]}
                    if set(names) != expected:
                        raise UpdateError("更新包包含未签名文件或缺失文件。")
                    target.mkdir()
                    for name, metadata in manifest["files"].items():
                        info = archive.getinfo("payload/" + name)
                        if info.file_size != metadata["size"] or stat.S_ISLNK(info.external_attr >> 16):
                            raise UpdateError("更新文件大小或类型无效。")
                        destination = target / name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        digest, size = hashlib.sha256(), 0
                        with archive.open(info) as source, destination.open("xb") as output:
                            while chunk := source.read(1024 * 1024):
                                size += len(chunk)
                                if size > metadata["size"]:
                                    raise UpdateError("更新文件超过声明大小。")
                                digest.update(chunk)
                                output.write(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                        if size != metadata["size"] or digest.hexdigest() != metadata["sha256"]:
                            raise UpdateError("更新文件校验失败。")
                    with (target / "update.json").open("xb") as output:
                        output.write(raw)
                        output.flush()
                        os.fsync(output.fileno())
                self.release(identifier)
                if healthcheck(target) is False:
                    raise UpdateError("更新版本自检失败。")
                _atomic_json(self.root / "state.json", {
                    "active": identifier, "previous": state["active"],
                    "highest_sequence": manifest["sequence"]})
                return manifest
            except BaseException as error:
                shutil.rmtree(target, ignore_errors=True)
                if isinstance(error, (BadZipFile, KeyError)):
                    raise UpdateError("更新包损坏或不完整。") from error
                raise

    def rollback(self, healthcheck):
        with self._lock():
            state = self.state()
            previous = state["previous"]
            if previous is not None:
                root, _ = self.release(previous)
                if healthcheck(root) is False:
                    raise UpdateError("回退版本自检失败。")
            elif state["active"] is None:
                raise UpdateError("没有可回退的更新。")
            _atomic_json(self.root / "state.json", {
                "active": previous, "previous": None,
                "highest_sequence": state["highest_sequence"]})
