"""Object-storage config for the cluster stage helper, read from environment.

On the cluster these come from the transient ``creds.env`` the runner worker drops per
job (0600, deleted by the sbatch trap) — the cluster never stores credentials. There is
no Yggdrasil-API config here: the cluster only moves bytes to/from object storage.
"""
import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Config:
    endpoint_url: str
    region: str
    access_key_id: str
    secret_access_key: str
    bucket: str
    use_ssl: bool
    verify_ssl: bool
    addressing_style: str
    key_prefix: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            endpoint_url=os.environ.get("OBJECT_STORAGE_ENDPOINT_URL", ""),
            region=os.environ.get("OBJECT_STORAGE_REGION", ""),
            access_key_id=os.environ.get("OBJECT_STORAGE_ACCESS_KEY_ID", ""),
            secret_access_key=os.environ.get("OBJECT_STORAGE_SECRET_ACCESS_KEY", ""),
            bucket=os.environ.get("OBJECT_STORAGE_BUCKET", ""),
            use_ssl=_bool("OBJECT_STORAGE_USE_SSL", True),
            verify_ssl=_bool("OBJECT_STORAGE_VERIFY_SSL", True),
            addressing_style=os.environ.get("OBJECT_STORAGE_ADDRESSING_STYLE", "path"),
            key_prefix=os.environ.get("OBJECT_STORAGE_KEY_PREFIX", "").strip("/"),
        )

    def require(self) -> None:
        missing = [
            n
            for n, v in (
                ("OBJECT_STORAGE_ENDPOINT_URL", self.endpoint_url),
                ("OBJECT_STORAGE_BUCKET", self.bucket),
                ("OBJECT_STORAGE_ACCESS_KEY_ID", self.access_key_id),
                ("OBJECT_STORAGE_SECRET_ACCESS_KEY", self.secret_access_key),
            )
            if not v
        ]
        if missing:
            raise SystemExit(f"Missing object-storage config: {', '.join(missing)}")
