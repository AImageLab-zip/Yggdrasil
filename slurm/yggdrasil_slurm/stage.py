"""``ygg-stage`` — move data between object storage and the cluster (boto3).

Used inside the sbatch job after sourcing the transient ``creds.env``:
  ygg-stage pull <key> <dest>     download one object (dest dir or file path)
  ygg-stage push <dir> <prefix>   upload every file under <dir> to <prefix>/<relpath>

The cluster never stores credentials — they come from the sourced env only.
"""
import os

import click

from yggdrasil_slurm.config import Config
from yggdrasil_slurm.storage import Storage


@click.group()
def main():
    pass


@main.command("pull")
@click.argument("key")
@click.argument("dest", type=click.Path())
def _pull(key, dest):
    cfg = Config.from_env()
    cfg.require()
    # A trailing slash (or an existing dir) means "into this directory".
    if dest.endswith(os.sep) or os.path.isdir(dest):
        dest = os.path.join(dest, os.path.basename(key.rstrip("/")))
    Storage(cfg).download(key, dest)
    click.echo(f"pulled {key} -> {dest}")


@main.command("push")
@click.argument("src_dir", type=click.Path(exists=True))
@click.argument("prefix")
def _push(src_dir, prefix):
    cfg = Config.from_env()
    cfg.require()
    storage = Storage(cfg)
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
