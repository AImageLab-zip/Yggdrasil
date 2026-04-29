import contextlib
import os
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from typing import BinaryIO, Dict, Generator, Optional, Tuple
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings


class ObjectStorageError(RuntimeError):
    pass


def _bool_env_fallback(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    content_length: Optional[int] = None
    content_type: Optional[str] = None
    etag: Optional[str] = None


class ObjectStorage:
    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: Optional[str] = None,
        region_name: Optional[str] = None,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        use_ssl: bool = True,
        verify_ssl: bool = True,
        addressing_style: str = "path",
        key_prefix: str = "",
    ):
        self.bucket = bucket
        self.endpoint_url = endpoint_url
        self.region_name = region_name or None
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.use_ssl = use_ssl
        self.verify_ssl = verify_ssl
        self.addressing_style = addressing_style
        self.key_prefix = key_prefix.strip("/")

        if not self.endpoint_url:
            raise ObjectStorageError("OBJECT_STORAGE_ENDPOINT_URL is not configured")

        parsed = urlparse(self.endpoint_url)
        if not parsed.scheme or not parsed.netloc:
            raise ObjectStorageError(
                "OBJECT_STORAGE_ENDPOINT_URL must include scheme and host, e.g. http://garage:3900"
            )

        secure = parsed.scheme == "https" or bool(self.use_ssl)
        verify = bool(self.verify_ssl) if secure else False

        session = boto3.session.Session()
        self._client = session.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            region_name=self.region_name,
            use_ssl=secure,
            verify=verify,
            config=Config(
                s3={"addressing_style": self.addressing_style or "path"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def normalize_key(self, key: str) -> str:
        key = (key or "").lstrip("/")
        if ".." in key.split("/"):
            raise ObjectStorageError("Invalid object key")
        if self.key_prefix:
            return f"{self.key_prefix}/{key}" if key else self.key_prefix
        return key

    def _client_error_code(self, exc: ClientError) -> str:
        return str((exc.response or {}).get("Error", {}).get("Code", ""))

    def head(self, key: str) -> ObjectInfo:
        key_n = self.normalize_key(key)
        try:
            resp = self._client.head_object(Bucket=self.bucket, Key=key_n)
        except ClientError as exc:
            code = self._client_error_code(exc)
            if code in {"NoSuchKey", "404", "NotFound"}:
                raise FileNotFoundError(key) from exc
            if code in {"NoSuchBucket"}:
                raise ObjectStorageError(
                    f"Bucket '{self.bucket}' does not exist"
                ) from exc
            raise ObjectStorageError(str(exc)) from exc

        etag = resp.get("ETag")
        if isinstance(etag, str):
            etag = etag.strip('"')
        return ObjectInfo(
            key=key,
            content_length=resp.get("ContentLength"),
            content_type=resp.get("ContentType"),
            etag=etag,
        )

    def ensure_bucket_exists(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.bucket)
            return
        except ClientError as exc:
            code = self._client_error_code(exc)
            if code not in {"404", "NoSuchBucket", "NotFound"}:
                raise ObjectStorageError(str(exc)) from exc

        try:
            kwargs = {"Bucket": self.bucket}
            if self.region_name and self.region_name not in {"us-east-1"}:
                kwargs["CreateBucketConfiguration"] = {
                    "LocationConstraint": self.region_name
                }
            self._client.create_bucket(**kwargs)
        except ClientError as exc:
            code = self._client_error_code(exc)
            if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise ObjectStorageError(str(exc)) from exc

    def exists(self, key: str) -> bool:
        try:
            self.head(key)
            return True
        except FileNotFoundError:
            return False

    def get(self, key: str) -> Tuple[BinaryIO, ObjectInfo]:
        key_n = self.normalize_key(key)
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=key_n)
        except ClientError as exc:
            code = self._client_error_code(exc)
            if code in {"NoSuchKey", "404", "NotFound"}:
                raise FileNotFoundError(key) from exc
            raise ObjectStorageError(str(exc)) from exc

        info = ObjectInfo(
            key=key,
            content_length=resp.get("ContentLength"),
            content_type=resp.get("ContentType"),
            etag=(resp.get("ETag") or "").strip('"') or None,
        )
        return resp["Body"], info

    def iter_bytes(
        self, key: str, *, chunk_size: int = 1024 * 1024
    ) -> Generator[bytes, None, None]:
        body, _ = self.get(key)
        try:
            while True:
                chunk = body.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            with contextlib.suppress(Exception):
                body.close()

    def upload_file(
        self,
        local_path: str,
        *,
        key: str,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
    ) -> ObjectInfo:
        key_n = self.normalize_key(key)
        extra: Dict[str, object] = {}
        if content_type:
            extra["ContentType"] = content_type
        if metadata:
            extra["Metadata"] = {str(k): str(v) for k, v in metadata.items()}

        self.ensure_bucket_exists()
        try:
            self._client.upload_file(local_path, self.bucket, key_n, ExtraArgs=extra)
        except ClientError as exc:
            raise ObjectStorageError(str(exc)) from exc
        return self.head(key)

    def upload_fileobj(
        self,
        fileobj: BinaryIO,
        *,
        key: str,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
    ) -> ObjectInfo:
        key_n = self.normalize_key(key)
        extra: Dict[str, object] = {}
        if content_type:
            extra["ContentType"] = content_type
        if metadata:
            extra["Metadata"] = {str(k): str(v) for k, v in metadata.items()}

        self.ensure_bucket_exists()
        try:
            self._client.upload_fileobj(fileobj, self.bucket, key_n, ExtraArgs=extra)
        except ClientError as exc:
            raise ObjectStorageError(str(exc)) from exc

        return self.head(key)

    def delete(self, key: str) -> None:
        key_n = self.normalize_key(key)
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key_n)
        except ClientError as exc:
            raise ObjectStorageError(str(exc)) from exc

    def list_keys(self, prefix: str) -> Generator[str, None, None]:
        prefix_n = self.normalize_key(prefix)
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix_n):
            for obj in page.get("Contents", []) or []:
                key_n = obj.get("Key")
                if not key_n:
                    continue
                if self.key_prefix and key_n.startswith(self.key_prefix + "/"):
                    yield key_n[len(self.key_prefix) + 1 :]
                else:
                    yield key_n

    def presign_get(self, key: str, *, expires_seconds: int = 600) -> str:
        key_n = self.normalize_key(key)
        try:
            return self._client.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": self.bucket, "Key": key_n},
                ExpiresIn=int(expires_seconds),
            )
        except ClientError as exc:
            raise ObjectStorageError(str(exc)) from exc


_storage_singleton: Optional[ObjectStorage] = None


def get_object_storage() -> ObjectStorage:
    global _storage_singleton
    if _storage_singleton is not None:
        return _storage_singleton

    bucket = getattr(settings, "OBJECT_STORAGE_BUCKET", None) or os.environ.get(
        "OBJECT_STORAGE_BUCKET"
    )
    if not bucket:
        raise ObjectStorageError("OBJECT_STORAGE_BUCKET is not configured")

    endpoint_url = getattr(settings, "OBJECT_STORAGE_ENDPOINT_URL", None)
    region_name = getattr(settings, "OBJECT_STORAGE_REGION", None)
    access_key_id = getattr(settings, "OBJECT_STORAGE_ACCESS_KEY_ID", None)
    secret_access_key = getattr(settings, "OBJECT_STORAGE_SECRET_ACCESS_KEY", None)
    use_ssl = _bool_env_fallback(
        getattr(settings, "OBJECT_STORAGE_USE_SSL", None), True
    )
    verify_ssl = _bool_env_fallback(
        getattr(settings, "OBJECT_STORAGE_VERIFY_SSL", None), True
    )
    addressing_style = getattr(settings, "OBJECT_STORAGE_ADDRESSING_STYLE", "path")
    key_prefix = getattr(settings, "OBJECT_STORAGE_KEY_PREFIX", "")

    _storage_singleton = ObjectStorage(
        bucket=bucket,
        endpoint_url=endpoint_url,
        region_name=region_name,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        use_ssl=use_ssl,
        verify_ssl=verify_ssl,
        addressing_style=addressing_style,
        key_prefix=key_prefix,
    )

    _storage_singleton.ensure_bucket_exists()
    return _storage_singleton


@contextlib.contextmanager
def download_to_tempfile(key: str, *, suffix: str = "") -> Generator[str, None, None]:
    storage = get_object_storage()
    fd, temp_path = tempfile.mkstemp(prefix="tf_obj_", suffix=suffix)
    os.close(fd)
    try:
        body, _ = storage.get(key)
        try:
            with open(temp_path, "wb") as f:
                while True:
                    chunk = body.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
        finally:
            with contextlib.suppress(Exception):
                body.close()
        yield temp_path
    finally:
        with contextlib.suppress(Exception):
            os.remove(temp_path)
