"""Opaque UUID object keys prevent user supplied filesystem/object paths."""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from app.ai.gateway import setting


class StorageError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _key(key: str) -> str:
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}(?:\.[a-z0-9]{1,8})?", key):
        raise StorageError("invalid_storage_key", "文件存储标识无效。")
    return key


def _new_key(suffix: str) -> str:
    if suffix and not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
        raise StorageError("invalid_storage_suffix", "文件扩展名无效。")
    return uuid.uuid4().hex + suffix.lower()


class LocalStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        path = self.root / _key(key)
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise StorageError("invalid_storage_path", "文件存储路径超出允许范围。")
        return path

    def put(self, data: bytes, *, suffix: str = "", content_type: str = "application/octet-stream") -> str:
        key = _new_key(suffix)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(key)
        try:
            # O_EXCL prevents an existing object from ever being silently replaced.
            with path.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise StorageError("storage_write_failed", "文件写入失败，请稍后重试。") from None
        return key

    def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise StorageError("storage_file_missing", "文件不存在或已被清理。") from None
        except OSError:
            raise StorageError("storage_read_failed", "文件读取失败，请稍后重试。") from None

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError:
            raise StorageError("storage_delete_failed", "文件删除失败，请稍后重试。") from None


class COSStorage:
    def __init__(self, settings, *, client=None):
        self.bucket = setting(settings, "cos_bucket", "")
        if client is not None:
            self.client = client
            return
        try:
            from qcloud_cos import CosConfig, CosS3Client
        except ImportError:
            raise StorageError("cos_dependency_missing", "COS 存储依赖未安装。") from None
        config = CosConfig(Region=setting(settings, "cos_region"),
                           SecretId=setting(settings, "cos_secret_id"),
                           SecretKey=setting(settings, "cos_secret_key"), Scheme="https")
        self.client = CosS3Client(config)

    def put(self, data: bytes, *, suffix: str = "", content_type: str = "application/octet-stream") -> str:
        key = _new_key(suffix)
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type,
                                   EnableMD5=True)
        except Exception:
            raise StorageError("storage_write_failed", "COS 文件写入失败，请稍后重试。") from None
        return key

    def get(self, key: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=_key(key))
            stream = response["Body"].get_raw_stream()
            try:
                return stream.read()
            finally:
                stream.close()
        except StorageError:
            raise
        except Exception:
            raise StorageError("storage_read_failed", "COS 文件读取失败，请稍后重试。") from None

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=_key(key))
        except StorageError:
            raise
        except Exception:
            raise StorageError("storage_delete_failed", "COS 文件删除失败，请稍后重试。") from None


def create_storage(settings):
    if setting(settings, "storage_backend", "local") == "cos":
        return COSStorage(settings)
    return LocalStorage(setting(settings, "storage_path", setting(settings, "storage_root", "var/storage")))
