"""Maintenance tasks. Routed to settings.MAINTENANCE_QUEUE - consumed only by
the local maintenance worker, never by external runners."""

import gzip
import logging
import subprocess
import tempfile
import time
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from celery import shared_task
from django.conf import settings

from common.object_storage import get_object_storage

logger = logging.getLogger(__name__)

# backups/mysql/<DB_NAME>_YYYYmmdd_HHMMSS.sql.gz
_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"


def _backup_key(now=None):
    now = now or datetime.now(dt_timezone.utc)
    prefix = settings.BACKUP_KEY_PREFIX
    return f"{prefix}{settings.DATABASES['default']['NAME']}_{now.strftime(_TIMESTAMP_FORMAT)}.sql.gz"


def _parse_backup_timestamp(key):
    """Timestamp from a backup key, or None if the key doesn't match."""
    stem = key.rsplit("/", 1)[-1]
    if not stem.endswith(".sql.gz"):
        return None
    try:
        ts = "_".join(stem[: -len(".sql.gz")].rsplit("_", 2)[-2:])
        return datetime.strptime(ts, _TIMESTAMP_FORMAT).replace(
            tzinfo=dt_timezone.utc
        )
    except ValueError:
        return None


def select_backups_to_delete(keys, *, keep_daily, keep_weekly, now=None):
    """Retention: keep the newest `keep_daily` backups, then at most one per
    ISO week for the next `keep_weekly` weeks; everything else is deleted.

    Pure function over key names so it is easily testable.
    """
    dated = [(k, ts) for k in keys if (ts := _parse_backup_timestamp(k))]
    dated.sort(key=lambda item: item[1], reverse=True)

    keep = set()
    for key, _ in dated[:keep_daily]:
        keep.add(key)

    weeks_kept = []
    for key, ts in dated[keep_daily:]:
        week = ts.isocalendar()[:2]
        if week not in weeks_kept and len(weeks_kept) < keep_weekly:
            weeks_kept.append(week)
            keep.add(key)

    return [key for key, _ in dated if key not in keep]


def _run_mysqldump(dump_path):
    db = settings.DATABASES["default"]
    cmd = [
        "mysqldump",
        f"--host={db['HOST'] or 'localhost'}",
        f"--port={db.get('PORT') or 3306}",
        f"--user={db['USER']}",
        "--single-transaction",
        "--routines",
        "--triggers",
        "--no-tablespaces",
        db["NAME"],
    ]
    with gzip.open(dump_path, "wb", compresslevel=6) as out:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"MYSQL_PWD": db["PASSWORD"]},
        )
        for chunk in iter(lambda: proc.stdout.read(1024 * 1024), b""):
            out.write(chunk)
        _, stderr = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"mysqldump exited {proc.returncode}: {stderr.decode(errors='replace')[:2000]}"
        )


def _verify_gzip(dump_path):
    """Decompress fully to prove integrity; returns uncompressed size."""
    size = 0
    with gzip.open(dump_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            size += len(chunk)
    if size == 0:
        raise RuntimeError("backup dump is empty")
    return size


@shared_task(name="common.tasks.backup_database")
def backup_database():
    """Dump the database, gzip, verify, upload to object storage, prune old
    backups, and record a SystemCheck row."""
    from common.models import SystemCheck

    started = time.monotonic()
    details = {}
    try:
        key = _backup_key()
        with tempfile.TemporaryDirectory() as tmp:
            dump_path = Path(tmp) / "backup.sql.gz"
            _run_mysqldump(dump_path)
            details["uncompressed_bytes"] = _verify_gzip(dump_path)
            details["compressed_bytes"] = dump_path.stat().st_size

            storage = get_object_storage()
            storage.upload_file(
                str(dump_path), key=key, content_type="application/gzip"
            )
        details["key"] = key

        deleted = prune_backups()
        details["pruned"] = deleted

        status = "ok"
        logger.info("Database backup uploaded to %s (%s)", key, details)
    except Exception as exc:
        status = "fail"
        details["error"] = str(exc)[:2000]
        logger.exception("Database backup failed")

    SystemCheck.objects.create(
        name="database_backup",
        status=status,
        duration_ms=int((time.monotonic() - started) * 1000),
        details=details,
    )
    return {"status": status, **details}


def prune_backups():
    """Apply the daily/weekly retention policy; returns deleted keys."""
    storage = get_object_storage()
    keys = list(storage.list_keys(settings.BACKUP_KEY_PREFIX))
    to_delete = select_backups_to_delete(
        keys,
        keep_daily=settings.BACKUP_KEEP_DAILY,
        keep_weekly=settings.BACKUP_KEEP_WEEKLY,
    )
    for key in to_delete:
        storage.delete(key)
        logger.info("Pruned old backup %s", key)
    return to_delete
