#!/usr/bin/env python3
"""Clone one S3/Garage bucket into another, key for key, server-side.

Written for the v1.9 -> 3.0 server migration: production's blobs live in a bucket the
new instance must not write to, so the new instance gets its own bucket holding the
same keys.

Both buckets are addressed through the *same* endpoint and credential, which is what
makes the copy server-side: ``CopyObject`` is a single request to the destination
naming the source, so no object bytes pass through this process or its network. A key
with read on the source and write on the destination is therefore the whole
requirement, and a 650 GiB clone costs this script almost nothing.

Keys are preserved exactly. That is only correct because Yggdrasil's key layout did not
change across the upgrade (``common/uploads.py`` derives prefixes from the *domain*, not
from anything the folders-to-projects migrations touched), so restored
``FileRegistry.file_path`` values resolve unchanged against the copy.
``OBJECT_STORAGE_KEY_PREFIX`` is deliberately ignored for the same reason.

Usage:
    python scripts/mirror_bucket.py --source toothfairy4m --dest yggdrasil
    python scripts/mirror_bucket.py --source ... --dest ... --dry-run
    python scripts/mirror_bucket.py --source ... --dest ... --skip-existing
    python scripts/mirror_bucket.py --source ... --dest ... --verify

Connection settings come from the OBJECT_STORAGE_* environment (the same names
settings.py reads), so inside the web container it is already configured. Override the
endpoint or credentials with --endpoint-url / --access-key-id / --secret-access-key when
copying between stores that do not share one.
"""

import argparse
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.object_storage import ObjectStorage, ObjectStorageError  # noqa: E402

GIB = 1024**3


def _bool(value, default):
    if value is None or value == "":
        return default
    return str(value).lower() in {"1", "true", "yes", "y", "on"}


def _storage(bucket, args):
    return ObjectStorage(
        bucket=bucket,
        endpoint_url=args.endpoint_url,
        region_name=args.region,
        access_key_id=args.access_key_id,
        secret_access_key=args.secret_access_key,
        use_ssl=args.use_ssl,
        verify_ssl=args.verify_ssl,
        addressing_style=args.addressing_style,
        key_prefix="",
        # Every worker needs its own connection, plus slack for retries; without this
        # --concurrency above botocore's default 10 buys nothing.
        max_pool_connections=max(args.concurrency + 8, 16),
        read_timeout=args.read_timeout,
    )


def _index(storage, prefix, label):
    """{key: size} for a whole bucket, printing progress -- 60k+ keys takes a moment."""
    index = {}
    started = time.monotonic()
    for info in storage.list_objects(prefix):
        index[info.key] = info.content_length or 0
        if len(index) % 20000 == 0:
            print(f"  ... {len(index)} keys listed in {label}", flush=True)
    total = sum(index.values())
    print(
        f"{label}: {len(index)} objects, {total / GIB:.2f} GiB "
        f"(listed in {time.monotonic() - started:.1f}s)",
        flush=True,
    )
    return index, total


def _verify(source_index, source_bytes, dest_index, dest_bytes):
    missing = sorted(set(source_index) - set(dest_index))
    mismatched = sorted(
        k for k in set(source_index) & set(dest_index)
        if source_index[k] != dest_index[k]
    )
    extra = sorted(set(dest_index) - set(source_index))

    print()
    print(f"source: {len(source_index)} objects, {source_bytes / GIB:.2f} GiB")
    print(f"dest:   {len(dest_index)} objects, {dest_bytes / GIB:.2f} GiB")
    print(f"missing from dest: {len(missing)}")
    print(f"size mismatch:     {len(mismatched)}")
    print(f"dest-only (extra): {len(extra)}")

    for label, keys in (("MISSING", missing), ("MISMATCH", mismatched)):
        for key in keys[:20]:
            print(f"  {label} {key}")
        if len(keys) > 20:
            print(f"  ... and {len(keys) - 20} more {label}")
    if extra:
        # Expected when the destination was not empty before the clone. Named, not
        # hidden, so it is a deliberate acceptance rather than an unnoticed difference.
        for key in extra[:10]:
            print(f"  extra {key}")
        if len(extra) > 10:
            print(f"  ... and {len(extra) - 10} more extra")

    return not missing and not mismatched


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--source", required=True, help="bucket to read from")
    parser.add_argument("--dest", required=True, help="bucket to write into")
    parser.add_argument("--prefix", default="", help="limit to one key prefix")
    parser.add_argument("--dry-run", action="store_true", help="list, write nothing")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument(
        "--read-timeout",
        type=int,
        default=900,
        help="seconds to wait for one copy request (default 900); CopyObject is "
        "synchronous, so this bounds the largest object rather than the network",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="skip keys already in dest with a matching size (resume, or catch drift)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="compare the two buckets and report drift; copies nothing",
    )
    parser.add_argument(
        "--endpoint-url",
        default=os.environ.get("OBJECT_STORAGE_ENDPOINT_URL"),
    )
    parser.add_argument("--region", default=os.environ.get("OBJECT_STORAGE_REGION"))
    parser.add_argument(
        "--access-key-id", default=os.environ.get("OBJECT_STORAGE_ACCESS_KEY_ID")
    )
    parser.add_argument(
        "--secret-access-key",
        default=os.environ.get("OBJECT_STORAGE_SECRET_ACCESS_KEY"),
    )
    parser.add_argument(
        "--addressing-style",
        default=os.environ.get("OBJECT_STORAGE_ADDRESSING_STYLE") or "path",
    )
    args = parser.parse_args()

    args.use_ssl = _bool(os.environ.get("OBJECT_STORAGE_USE_SSL"), False)
    args.verify_ssl = _bool(os.environ.get("OBJECT_STORAGE_VERIFY_SSL"), True)

    if args.source == args.dest:
        parser.error("--source and --dest must differ")
    if not args.endpoint_url:
        parser.error("OBJECT_STORAGE_ENDPOINT_URL is not set and --endpoint-url was not given")

    source = _storage(args.source, args)
    dest = _storage(args.dest, args)

    print(f"endpoint {args.endpoint_url}   {args.source} -> {args.dest}")
    if args.prefix:
        print(f"prefix   {args.prefix!r}")
    print()

    source_index, source_bytes = _index(source, args.prefix, f"source {args.source}")

    if args.verify:
        dest_index, dest_bytes = _index(dest, args.prefix, f"dest   {args.dest}")
        return 0 if _verify(source_index, source_bytes, dest_index, dest_bytes) else 1

    # Shuffled, not sorted. Keys sort by prefix, and size correlates strongly with
    # prefix -- ``exports/`` is multi-GB zips, ``maxillo/processed/cbct/`` hundreds of
    # MB, ``raw/cbct/`` ~1 MB folder members. Copying in key order therefore points every
    # worker at the same size class at once, so a run of giant objects blocks the whole
    # pool for minutes while the store thrashes. Interleaving sizes keeps throughput and
    # visible progress steady. Seeded so a resumed run is reproducible.
    todo = sorted(source_index)
    random.Random(0).shuffle(todo)
    if args.skip_existing:
        dest_index, _ = _index(dest, args.prefix, f"dest   {args.dest}")
        todo = [
            key
            for key in todo
            if dest_index.get(key) != source_index[key]
        ]
        print(f"{len(source_index) - len(todo)} already present, {len(todo)} to copy")

    todo_bytes = sum(source_index[k] for k in todo)
    print(f"\nto copy: {len(todo)} objects, {todo_bytes / GIB:.2f} GiB")
    if args.dry_run:
        for key in todo[:20]:
            print(f"  would copy {key} ({source_index[key]} bytes)")
        if len(todo) > 20:
            print(f"  ... and {len(todo) - 20} more")
        print("\n--dry-run: nothing written")
        return 0
    if not todo:
        print("nothing to do")
        return 0

    lock = threading.Lock()
    state = {"done": 0, "bytes": 0, "last": time.monotonic()}
    failures = []
    started = time.monotonic()

    def copy_one(key):
        size = source_index[key]
        try:
            dest.copy_from(
                source_bucket=args.source, source_key=key, dest_key=key, size=size
            )
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            with lock:
                failures.append((key, str(exc)))
                # Printed as it happens, not only in the summary: a clone of this size
                # runs for hours, and a failure mode worth reacting to (an overloaded
                # store, a broken multipart path) has to be visible while there is
                # still a run to change. Capped so a systemic failure cannot flood.
                if len(failures) <= 10:
                    print(f"  FAIL {key} ({size} bytes): {exc}", flush=True)
                elif len(failures) == 11:
                    print("  (further failures summarised at the end)", flush=True)
            return
        with lock:
            state["done"] += 1
            state["bytes"] += size

    stop_heartbeat = threading.Event()

    def heartbeat():
        while not stop_heartbeat.wait(30):
            with lock:
                done, copied = state["done"], state["bytes"]
            elapsed = time.monotonic() - started
            rate = copied / elapsed if elapsed else 0
            remaining = (todo_bytes - copied) / rate / 3600 if rate else float("inf")
            print(
                f"  [{elapsed / 60:5.1f} min] {done}/{len(todo)} objects, "
                f"{copied / GIB:.2f}/{todo_bytes / GIB:.2f} GiB, "
                f"{rate / GIB * 1024:.0f} MiB/s, {len(failures)} failed, "
                f"~{remaining:.1f}h left",
                flush=True,
            )

    beat = threading.Thread(target=heartbeat, daemon=True)
    beat.start()
    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [pool.submit(copy_one, key) for key in todo]
            for _ in as_completed(futures):
                pass
    finally:
        stop_heartbeat.set()

    elapsed = time.monotonic() - started
    print(
        f"\ncopied {state['done']}/{len(todo)} objects, "
        f"{state['bytes'] / GIB:.2f} GiB in {elapsed / 60:.1f} min"
    )

    if failures:
        print(f"\n{len(failures)} FAILURES:")
        for key, err in failures[:50]:
            print(f"  {key}: {err}")
        if len(failures) > 50:
            print(f"  ... and {len(failures) - 50} more")

    # The count/size comparison is the only real proof the clone is complete, so it
    # always runs -- a copy loop that reports success per object can still be short.
    print("\nre-listing destination to confirm:")
    dest_index, dest_bytes = _index(dest, args.prefix, f"dest   {args.dest}")
    complete = _verify(source_index, source_bytes, dest_index, dest_bytes)

    if failures or not complete:
        print("\nINCOMPLETE - re-run with --skip-existing to retry only what is missing")
        return 1
    print("\nclone complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
