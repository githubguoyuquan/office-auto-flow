"""Atomic license import and task admission; no private signing material."""
import os
from pathlib import Path
import secrets
import threading

from license_keys import PUBLIC_KEYS
from offline_license import (LicenseError, MAX_LICENSE_BYTES, PRODUCT, TrustedClock,
                             device_id, validate_license,
                             verify_license, _timestamp)


class LicenseManager:
    def __init__(self, directory, public_keys=None, clock=None, device=None, *, unrestricted=False):
        self.unrestricted = unrestricted
        self.directory = directory
        self.keys = PUBLIC_KEYS if public_keys is None else public_keys
        self.clock = clock or TrustedClock()
        self._device = device
        self.lock = threading.RLock()

    @property
    def device(self):
        if self._device is None:
            self._device = device_id()
        return self._device

    @property
    def path(self):
        return Path(self.directory()) / "authorization.license"

    def request(self):
        return {"version": 1, "product": PRODUCT, "device_id": self.device,
                "request_id": secrets.token_hex(16)}

    def _read(self):
        with self.path.open("rb") as stream:
            content = stream.read(MAX_LICENSE_BYTES + 1)
        return content

    def require_valid(self, force=False, *, feature=None):
        if self.unrestricted:
            return self.status()
        with self.lock:
            if not self.keys:
                raise LicenseError("此工作台尚未配置签发公钥，请联系管理员构建授权版本。")
            try:
                content = self._read()
            except FileNotFoundError as error:
                raise LicenseError("尚未导入授权文件，请先在授权管理中激活。") from error
            # Verify before any time request, so invalid files cause no network access.
            verify_license(content, self.keys, self.device)
            if force:
                self.clock.now(force=True)
            return validate_license(content, self.keys, self.device, self.clock, feature=feature)

    def install(self, content):
        with self.lock:
            if not self.keys:
                raise LicenseError("此工作台尚未配置签发公钥，请联系管理员。")
            verify_license(content, self.keys, self.device)
            self.clock.now(force=True)
            result = validate_license(content, self.keys, self.device, self.clock, feature=None)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".license-{secrets.token_hex(8)}.tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(content)
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
            return result

    def status(self):
        """Polling reads local state only; the explicit check/task endpoints do I/O."""
        if self.unrestricted:
            return {"valid": True, "code": "unrestricted", "unrestricted": True,
                    "device_id": "", "features": [], "message": "当前版本全部模块开放，无需导入许可证。"}
        result = {"valid": False, "code": "unlicensed", "device_id": "", "features": []}
        try:
            result["device_id"] = self.device
            if not self.keys:
                raise LicenseError("尚未配置签发公钥，请联系管理员。")
            with self.lock:
                content = self._read()
                payload = verify_license(content, self.keys, self.device)
                result.update(license_id=payload["license_id"], expires_at=payload["expires_at"],
                              customer=payload.get("customer", ""), features=list(payload["features"]))
                try:
                    now = self.clock.cached_now()
                except LicenseError:
                    return dict(result, code="time_check_required", message="需要联网校验时间。")
                if now < max(_timestamp(payload["issued_at"]), _timestamp(payload["not_before"])):
                    raise LicenseError("授权尚未生效。")
                if now >= _timestamp(payload["expires_at"]):
                    raise LicenseError("授权已到期，请联系管理员续期。")
                return dict(result, valid=True, code="valid", message="授权有效",
                            remaining_seconds=int(_timestamp(payload["expires_at"]) - now))
        except FileNotFoundError:
            return dict(result, message="尚未导入授权文件。")
        except (LicenseError, OSError, ValueError) as error:
            return dict(result, message=str(error))
