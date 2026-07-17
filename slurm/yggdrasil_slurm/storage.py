"""Minimal S3-compatible client for the cluster runner.

Mirrors the relevant behavior of the web app's ``common.object_storage.ObjectStorage``
(path-style addressing, key prefix, Garage checksum relaxation) but stands alone so the
cluster env needs only ``boto3`` — not Django.
"""
import os
from urllib.parse import urlparse

import boto3
from botocore.config import Config as BotoConfig

from yggdrasil_slurm.config import Config


class Storage:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bucket = cfg.bucket
        self.key_prefix = (cfg.key_prefix or "").strip("/")

        parsed = urlparse(cfg.endpoint_url)
        secure = parsed.scheme == "https" or cfg.use_ssl
        verify = cfg.verify_ssl if secure else False
        self._client = boto3.session.Session().client(
            "s3",
            endpoint_url=cfg.endpoint_url,
            aws_access_key_id=cfg.access_key_id,
            aws_secret_access_key=cfg.secret_access_key,
            region_name=cfg.region or None,
            use_ssl=secure,
            verify=verify,
            config=BotoConfig(
                s3={"addressing_style": cfg.addressing_style or "path"},
                retries={"max_attempts": 3, "mode": "standard"},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    def _key(self, key: str) -> str:
        key = (key or "").lstrip("/")
        if self.key_prefix:
            return f"{self.key_prefix}/{key}" if key else self.key_prefix
        return key

    def download(self, key: str, dest_path: str) -> None:
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        self._client.download_file(self.bucket, self._key(key), dest_path)

    def upload(self, local_path: str, key: str, content_type: str | None = None) -> None:
        extra = {"ContentType": content_type} if content_type else {}
        self._client.upload_file(local_path, self.bucket, self._key(key), ExtraArgs=extra)
