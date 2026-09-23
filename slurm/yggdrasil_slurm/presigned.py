"""Object-storage access through presigned requests only (``YGG_STAGE_MODE=presigned``).

The runner worker hands each job ``YGG_STAGE_GRANTS`` (JSON): a presigned GET URL per
input key, and a POST policy that accepts uploads only under the job's output prefix.
The job holds no storage credentials, so it can read its own inputs and write its own
outputs and nothing else, and only until the grants expire.
"""
import io
import json
import os
import secrets
import time

import urllib3

_CHUNK = 8 * 1024 * 1024
_ATTEMPTS = 3


class GrantError(SystemExit):
    pass


class _MultipartBody(io.RawIOBase):
    """A multipart/form-data body streamed from disk, with a known length.

    Fields come first and the file last, as S3 POST requires. Streaming matters:
    job outputs are volumes and videos, and the stock encoders read the whole file.
    """

    def __init__(self, fields, file_path, filename):
        self.boundary = "ygg" + secrets.token_hex(16)
        head = io.BytesIO()
        for name, value in fields.items():
            head.write(
                f'--{self.boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode()
            )
        head.write(
            f'--{self.boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
        )
        self._parts = [
            io.BytesIO(head.getvalue()),
            open(file_path, "rb"),
            io.BytesIO(f"\r\n--{self.boundary}--\r\n".encode()),
        ]
        self.length = len(head.getvalue()) + os.path.getsize(file_path) + len(self._parts[2].getvalue())

    def readable(self):
        return True

    def readinto(self, buffer):
        while self._parts:
            n = self._parts[0].readinto(buffer)
            if n:
                return n
            self._parts.pop(0).close()
        return 0

    def close(self):
        for part in self._parts:
            part.close()
        self._parts = []
        super().close()


class PresignedStorage:
    def __init__(self, grants):
        self.inputs = dict(grants.get("inputs") or {})
        output = grants.get("output") or {}
        self.output_prefix = (output.get("prefix") or "").strip("/")
        self.key_prefix = output.get("key_prefix") or ""
        self.post_url = output.get("url") or ""
        self.post_fields = dict(output.get("fields") or {})
        self._http = urllib3.PoolManager(
            retries=urllib3.Retry(total=_ATTEMPTS, backoff_factor=2, allowed_methods={"GET"})
        )

    @classmethod
    def from_env(cls):
        raw = os.environ.get("YGG_STAGE_GRANTS", "")
        if not raw:
            raise GrantError("YGG_STAGE_MODE=presigned but YGG_STAGE_GRANTS is empty")
        return cls(json.loads(raw))

    def download(self, key, dest_path):
        url = self.inputs.get(key)
        if not url:
            raise GrantError(f"no presigned grant for input {key!r}")
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        response = self._http.request("GET", url, preload_content=False)
        try:
            if response.status != 200:
                raise GrantError(f"GET {key} failed: HTTP {response.status} {response.data[:200]!r}")
            with open(dest_path, "wb") as out:
                for chunk in response.stream(_CHUNK):
                    out.write(chunk)
        finally:
            response.release_conn()

    def upload(self, local_path, key, content_type=None):
        key = key.strip("/")
        if not key.startswith(self.output_prefix + "/"):
            raise GrantError(f"{key!r} is outside this job's output prefix {self.output_prefix!r}/")
        # content_type is ignored: the signed policy admits no field beyond key/file.
        fields = {**self.post_fields, "key": self.key_prefix + key[len(self.output_prefix) + 1:]}
        for attempt in range(1, _ATTEMPTS + 1):
            body = _MultipartBody(fields, local_path, os.path.basename(local_path))
            try:
                response = self._http.request(
                    "POST",
                    self.post_url,
                    body=body,
                    headers={
                        "Content-Type": f"multipart/form-data; boundary={body.boundary}",
                        "Content-Length": str(body.length),
                    },
                    retries=False,
                )
            except urllib3.exceptions.HTTPError as exc:
                error = str(exc)
            else:
                if response.status in (200, 201, 204):
                    return
                error = f"HTTP {response.status} {response.data[:200]!r}"
                if response.status < 500:
                    break
            finally:
                body.close()
            time.sleep(2 ** attempt)
        raise GrantError(f"POST {key} failed: {error}")
