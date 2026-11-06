"""Verify device-bound signed licenses using independently fetched HTTPS time.

Only public verification keys belong in the customer application. Signing keys
and the issuing tool must be distributed separately. No license/device data is
sent to the time sources. This module does not claim resistance to patched code
or a compromised operating system/TLS trust store.
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path
import re
import secrets
import ssl
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, HTTPSHandler


PRODUCT = "office-auto-flow-api"
MAX_LICENSE_BYTES = 64 * 1024
TIME_CHECK_INTERVAL = 24 * 60 * 60
TIME_SKEW = 120
TIME_SOURCES = (
    "https://www.aliyun.com/",
    "https://www.microsoft.com/",
    "https://www.cloudflare.com/",
)


class LicenseError(ValueError):
    """A customer-facing license or trusted-time validation failure."""


def canonical_payload(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def device_id() -> str:
    """Use a stable OS installation identifier, never MAC addresses/host names."""
    if sys.platform == "win32":
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            identifier = str(winreg.QueryValueEx(key, "MachineGuid")[0]).strip()
    elif sys.platform == "darwin":
        result = subprocess.run(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                capture_output=True, text=True, timeout=10, check=True)
        match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', result.stdout)
        identifier = match[1] if match else ""
    else:
        identifier = Path("/etc/machine-id").read_text(encoding="ascii").strip()
    if not identifier:
        raise LicenseError("无法读取稳定的设备标识，请联系管理员。")
    digest = hashlib.sha256(f"{PRODUCT}:{sys.platform}:{identifier.lower()}".encode()).hexdigest()
    return "AIOA-" + digest.upper()


def activation_request() -> dict:
    return {"version": 1, "product": PRODUCT, "device_id": device_id(),
            "request_id": secrets.token_hex(16)}


def _timestamp(value: object) -> float:
    if not isinstance(value, str):
        raise LicenseError("授权文件的时间字段无效。")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("timezone is required")
        return parsed.timestamp()
    except (ValueError, OverflowError) as error:
        raise LicenseError("授权文件的时间字段无效。") from error


def _unique_object(pairs: list) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise LicenseError("授权文件包含重复字段。")
        value[key] = item
    return value


def verify_license(document: bytes, public_keys: dict[str, bytes],
                   expected_device: str) -> dict:
    """Verify signature and schema; trusted-time checks are performed separately."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if len(document) > MAX_LICENSE_BYTES:
        raise LicenseError("授权文件过大。")
    try:
        envelope = json.loads(document, object_pairs_hook=_unique_object)
        if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature"}:
            raise ValueError("invalid envelope")
        payload = envelope["payload"]
        if not isinstance(payload, dict):
            raise ValueError("invalid payload")
        key_id = payload.get("key_id")
        if not isinstance(key_id, str) or key_id not in public_keys:
            raise LicenseError("授权签发者不受信任，请使用匹配版本的授权文件。")
        signature = base64.b64decode(envelope["signature"], validate=True)
        Ed25519PublicKey.from_public_bytes(public_keys[key_id]).verify(signature, canonical_payload(payload))
    except LicenseError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, InvalidSignature) as error:
        raise LicenseError("授权文件无效或已被修改。") from error
    if type(payload.get("version")) is not int or payload["version"] != 1:
        raise LicenseError("不支持此授权文件版本。")
    if payload.get("product") != PRODUCT or payload.get("device_id") != expected_device:
        raise LicenseError("授权文件与当前产品或设备不匹配。")
    if not isinstance(payload.get("license_id"), str) or not payload["license_id"].strip():
        raise LicenseError("授权编号无效。")
    features = payload.get("features")
    if not isinstance(features, list) or not features or any(not isinstance(item, str) or not item for item in features):
        raise LicenseError("授权功能列表无效。")
    issued, start, end = (_timestamp(payload.get(field)) for field in ("issued_at", "not_before", "expires_at"))
    if not issued <= start < end:
        raise LicenseError("授权生效或到期时间无效。")
    return payload


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _https_timestamp(url: str) -> float:
    if urlsplit(url).scheme != "https":
        raise LicenseError("时间源必须使用 HTTPS。")
    opener = build_opener(HTTPSHandler(context=ssl.create_default_context()), _NoRedirect())
    request = Request(url + "?aioa_time=" + secrets.token_hex(12), method="HEAD",
                      headers={"Cache-Control": "no-cache, no-store", "User-Agent": "AIOA-TimeCheck/1"})
    with opener.open(request, timeout=5) as response:
        if response.status != 200 or float(response.headers.get("Age", "0")) > 5:
            raise LicenseError("时间源返回了缓存或异常响应。")
        date = parsedate_to_datetime(response.headers["Date"])
        if date.utcoffset() is None:
            raise LicenseError("时间源缺少时区。")
        return date.timestamp()


def fetch_network_time() -> float:
    """Require two different HTTPS hosts agreeing within two minutes."""
    readings = []
    with ThreadPoolExecutor(max_workers=len(TIME_SOURCES)) as pool:
        futures = [pool.submit(_https_timestamp, url) for url in TIME_SOURCES]
        for future in as_completed(futures):
            try:
                timestamp = future.result()
                if timestamp > 0:
                    readings.append(timestamp)
            except Exception:
                continue
    groups = [[other for other in readings if reading <= other <= reading + TIME_SKEW]
              for reading in readings]
    agreement = max(groups, key=len, default=[])
    if len(agreement) < 2:
        raise LicenseError("联网时间校验失败，请检查网络、系统时间或代理设置后重试。")
    return max(agreement)


class TrustedClock:
    """Network anchor per application process, refreshed at least every 24 hours.

    No editable on-disk timestamp is accepted as a trusted anchor. Local clock
    jumps force online validation. Failure never extends the cached interval.
    """

    def __init__(self, fetch_time=fetch_network_time, monotonic=time.monotonic, wall_time=time.time):
        self._fetch_time = fetch_time
        self._monotonic = monotonic
        self._wall_time = wall_time
        self._lock = threading.Lock()
        self._anchor = None
        self._last = None

    def now(self, force: bool = False) -> float:
        with self._lock:
            mono, wall = self._monotonic(), self._wall_time()
            elapsed = 0 if self._anchor is None else mono - self._anchor[1]
            drift = 0 if self._anchor is None else abs((wall - self._anchor[2]) - elapsed)
            if (force or self._anchor is None or elapsed < 0
                    or elapsed >= TIME_CHECK_INTERVAL or drift > TIME_SKEW):
                trusted = self._fetch_time()
                if self._last is not None and trusted < self._last - TIME_SKEW:
                    raise LicenseError("联网时间早于最近一次校验时间，请检查网络后重试。")
                trusted = max(trusted, self._last or trusted)
                self._anchor = (trusted, self._monotonic(), self._wall_time())
                elapsed = 0
            current = self._anchor[0] + elapsed
            self._last = max(current, self._last or current)
            return self._last


def validate_license(document: bytes, public_keys: dict[str, bytes], expected_device: str,
                     clock: TrustedClock, feature: str = "process_sheet") -> dict:
    payload = verify_license(document, public_keys, expected_device)
    if feature not in payload["features"]:
        raise LicenseError("当前授权不包含此功能。")
    now = clock.now()
    if now < _timestamp(payload["issued_at"]) or now < _timestamp(payload["not_before"]):
        raise LicenseError("授权尚未生效。")
    if now >= _timestamp(payload["expires_at"]):
        raise LicenseError("授权已到期，请联系管理员续期。")
    return {"license_id": payload["license_id"], "expires_at": payload["expires_at"],
            "remaining_seconds": int(_timestamp(payload["expires_at"]) - now),
            "checked_at": datetime.fromtimestamp(now, timezone.utc).isoformat()}
