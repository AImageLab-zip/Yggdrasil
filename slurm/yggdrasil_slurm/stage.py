"""``ygg-stage`` — move data between object storage and the cluster.

Used inside the sbatch job after sourcing the transient ``creds.env``:
  ygg-stage pull <key> <dest>     download one object (dest dir or file path)
  ygg-stage push <dir> <prefix>   upload every file under <dir> to <prefix>/<relpath>

``YGG_STAGE_MODE`` in that file picks the backend: ``presigned`` (per-job presigned
URLs, no credentials at all -- see ``presigned.py``) or, for runners that predate it,
``credentials`` / unset (the ``OBJECT_STORAGE_*`` keys, via boto3).
"""
import os

import click

from yggdrasil_slurm.config import Config


def _storage():
    if os.environ.get("YGG_STAGE_MODE", "").strip() == "presigned":
        from yggdrasil_slurm.presigned import PresignedStorage

        return PresignedStorage.from_env()
    from yggdrasil_slurm.storage import Storage

    cfg = Config.from_env()
    cfg.require()
    return Storage(cfg)


@click.group()
def main():
    pass


@main.command("pull")
@click.argument("key")
@click.argument("dest", type=click.Path())
def _pull(key, dest):
    # A trailing slash (or an existing dir) means "into this directory".
    if dest.endswith(os.sep) or os.path.isdir(dest):
        dest = os.path.join(dest, os.path.basename(key.rstrip("/")))
    _storage().download(key, dest)
    click.echo(f"pulled {key} -> {dest}")


@main.command("push")
@click.argument("src_dir", type=click.Path(exists=True))
@click.argument("prefix")
def _push(src_dir, prefix):
    storage = _storage()
    prefix = prefix.strip("/")
    count = 0
    for dirpath, _dirs, files in os.walk(src_dir):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, src_dir).replace(os.sep, "/")
            storage.upload(full, f"{prefix}/{rel}")
            count += 1
    click.echo(f"pushed {count} file(s) from {src_dir} -> {prefix}/")


if __name__ == "__main__":
    main()
