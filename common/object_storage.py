import contextlib
import os
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from typing import BinaryIO, Dict, Generator, Optional, Tuple
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
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
        max_pool_connections: Optional[int] = None,
        read_timeout: Optional[int] = None,
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
                # botocore's default pool is 10 connections; a caller fanning out wider
                # than that silently serialises on the pool instead of the network, so
                # a bulk copy has to raise it or its concurrency is a fiction.
                max_pool_connections=max_pool_connections or 10,
                # Default (60s) is right for serving a file to a browser. A caller
                # driving server-side copies needs far more: CopyObject is
                # *synchronous* -- the store does not answer until the object is fully
                # copied -- so a few hundred MB legitimately outlives 60s, and timing
                # out mid-copy makes the retry redo work the store is still doing.
                read_timeout=read_timeout or 60,
                # Garage can advertise flexible checksums that botocore then
                # rejects on plain get_object reads. Keep validation only for
                # operations that explicitly require it.
                # An optimization for video stream
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
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
        except BotoCoreError as exc:
            # Transport-level failure (DNS, connection refused, timeout): the
            # store is unreachable, not merely missing the key. Normalize to
            # ObjectStorageError so callers with an unreachable-store policy
            # (e.g. file_access.exists) see one exception type. CI has no
            # object storage; without this a ClientError-only except lets
            # EndpointConnectionError escape raw.
            raise ObjectStorageError(str(exc)) from exc
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
        except BotoCoreError as exc:
            raise ObjectStorageError(str(exc)) from exc
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
        except BotoCoreError as exc:
            raise ObjectStorageError(str(exc)) from exc
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
        except BotoCoreError as exc:
            raise ObjectStorageError(str(exc)) from exc
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

    def get_range(self, key: str, byte_range: str) -> Tuple[BinaryIO, ObjectInfo]:
        """Fetch a byte range of an object (e.g. byte_range='bytes=0-1023')."""
        key_n = self.normalize_key(key)
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=key_n, Range=byte_range)
        except BotoCoreError as exc:
            raise ObjectStorageError(str(exc)) from exc
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
        except BotoCoreError as exc:
            raise ObjectStorageError(str(exc)) from exc
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
        except BotoCoreError as exc:
            raise ObjectStorageError(str(exc)) from exc
        except ClientError as exc:
            raise ObjectStorageError(str(exc)) from exc

        return self.head(key)

    def delete(self, key: str) -> None:
        key_n = self.normalize_key(key)
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key_n)
        except BotoCoreError as exc:
            raise ObjectStorageError(str(exc)) from exc
        except ClientError as exc:
            raise ObjectStorageError(str(exc)) from exc

    def list_objects(self, prefix: str = "") -> Generator[ObjectInfo, None, None]:
        """Every object under ``prefix``, with its size.

        ``list_keys`` answers "what is there"; a bucket-to-bucket copy also has to know
        how big each object is, because the server-side copy path forks on the 5 GiB
        ``CopySource`` limit. Same prefix handling as ``list_keys``.
        """
        prefix_n = self.normalize_key(prefix) if prefix else self.key_prefix
        kwargs = {"Bucket": self.bucket}
        if prefix_n:
            kwargs["Prefix"] = prefix_n
        paginator = self._client.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(**kwargs):
                for obj in page.get("Contents", []) or []:
                    key_n = obj.get("Key")
                    if not key_n:
                        continue
                    if self.key_prefix and key_n.startswith(self.key_prefix + "/"):
                        key = key_n[len(self.key_prefix) + 1 :]
                    else:
                        key = key_n
                    etag = obj.get("ETag")
                    if isinstance(etag, str):
                        etag = etag.strip('"')
                    yield ObjectInfo(
                        key=key, content_length=obj.get("Size"), etag=etag
                    )
        except BotoCoreError as exc:
            raise ObjectStorageError(str(exc)) from exc
        except ClientError as exc:
            raise ObjectStorageError(str(exc)) from exc

    # S3 refuses a single CopyObject above 5 GiB; above it the copy must be assembled
    # from ranged UploadPartCopy calls. 5 GiB exactly is the documented boundary.
    COPY_OBJECT_MAX_BYTES = 5 * 1024**3
    COPY_PART_BYTES = 128 * 1024**2

    def copy_from(
        self,
        *,
        source_bucket: str,
        source_key: str,
        dest_key: str,
        size: Optional[int] = None,
    ) -> None:
        """Copy one object into this bucket, server-side.

        The bytes never travel through this process: ``CopyObject`` is a single request
        to the *destination* naming the source, so one credential granted read on the
        source and write on the destination moves the object entirely inside the store.
        That is the whole reason this exists rather than a get/put loop.

        ``size`` selects the path when known -- pass it from a ``list_objects`` sweep to
        avoid a HEAD per object. Without it, an object over the limit is discovered by
        the failed single copy and retried as multipart.
        """
        dest_key_n = self.normalize_key(dest_key)
        copy_source = {"Bucket": source_bucket, "Key": source_key}

        if size is None or size <= self.COPY_OBJECT_MAX_BYTES:
            try:
                self._client.copy_object(
                    Bucket=self.bucket, Key=dest_key_n, CopySource=copy_source
                )
                return
            except BotoCoreError as exc:
                raise ObjectStorageError(
                    f"copy {source_bucket}/{source_key} -> {self.bucket}/{dest_key_n}: {exc}"
                ) from exc
            except ClientError as exc:
                code = self._client_error_code(exc)
                # Only "too big" is worth retrying as multipart; anything else is real.
                if size is not None or code not in {
                    "InvalidRequest",
                    "EntityTooLarge",
                    "InvalidArgument",
                }:
                    raise ObjectStorageError(
                        f"copy {source_bucket}/{source_key} -> {self.bucket}/{dest_key_n}: {exc}"
                    ) from exc
                size = self.head_source(source_bucket, source_key)

        self._multipart_copy(
            copy_source=copy_source,
            dest_key_n=dest_key_n,
            size=size,
            label=f"{source_bucket}/{source_key}",
        )

    def head_source(self, bucket: str, key: str) -> int:
        """Size of an object in an arbitrary bucket this client can read."""
        try:
            return int(self._client.head_object(Bucket=bucket, Key=key)["ContentLength"])
        except BotoCoreError as exc:
            raise ObjectStorageError(f"head {bucket}/{key}: {exc}") from exc
        except ClientError as exc:
            raise ObjectStorageError(f"head {bucket}/{key}: {exc}") from exc

    def _multipart_copy(self, *, copy_source, dest_key_n, size, label) -> None:
        try:
            upload_id = self._client.create_multipart_upload(
                Bucket=self.bucket, Key=dest_key_n
            )["UploadId"]
        except BotoCoreError as exc:
            raise ObjectStorageError(f"multipart init for {label}: {exc}") from exc
        except ClientError as exc:
            raise ObjectStorageError(f"multipart init for {label}: {exc}") from exc

        parts = []
        try:
            offset = 0
            number = 1
            while offset < size:
                last = min(offset + self.COPY_PART_BYTES, size) - 1
                resp = self._client.upload_part_copy(
                    Bucket=self.bucket,
                    Key=dest_key_n,
                    UploadId=upload_id,
                    PartNumber=number,
                    CopySource=copy_source,
                    CopySourceRange=f"bytes={offset}-{last}",
                )
                parts.append(
                    {"ETag": resp["CopyPartResult"]["ETag"], "PartNumber": number}
                )
                offset = last + 1
                number += 1
            self._client.complete_multipart_upload(
                Bucket=self.bucket,
                Key=dest_key_n,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except Exception as exc:
            # An abandoned multipart upload holds storage indefinitely, so abort before
            # surfacing the failure -- and never let the abort mask the original error.
            try:
                self._client.abort_multipart_upload(
                    Bucket=self.bucket, Key=dest_key_n, UploadId=upload_id
                )
            except ClientError:
                pass
            raise ObjectStorageError(f"multipart copy of {label}: {exc}") from exc

    def list_keys(self, prefix: str) -> Generator[str, None, None]:
        prefix_n = self.normalize_key(prefix)
        paginator = self._client.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix_n):
                for obj in page.get("Contents", []) or []:
                    key_n = obj.get("Key")
                    if not key_n:
                        continue
                    if self.key_prefix and key_n.startswith(self.key_prefix + "/"):
                        yield key_n[len(self.key_prefix) + 1 :]
                    else:
                        yield key_n
        except BotoCoreError as exc:
            raise ObjectStorageError(str(exc)) from exc
        except ClientError as exc:
            raise ObjectStorageError(str(exc)) from exc

    def presign_get(self, key: str, *, expires_seconds: int = 600) -> str:
        key_n = self.normalize_key(key)
        try:
            return self._client.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": self.bucket, "Key": key_n},
                ExpiresIn=int(expires_seconds),
            )
        except BotoCoreError as exc:
            raise ObjectStorageError(str(exc)) from exc
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
