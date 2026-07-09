# Yggdrasil SLURM stage helper (cluster side)

Cluster-side counterpart to Yggdrasil 2.0's runner worker. The **runner worker** (a
Celery worker in the Yggdrasil deploy) claims a job, SSHes in, drops a transient
per-job `creds.env`, and `sbatch`es `ALGO_BASE_DIR/<algo_name>/run.sbatch`. That script
uses `ygg-stage` (this package) to pull inputs and push outputs **directly** between
object storage and the cluster — no data flows through the worker, and the cluster
stores no credentials.

Per-algo scripts and layout conventions live one level up, at
`/work/yggdrasil_workers/README.md` — read that first if you're wiring up a new algo.
This directory only holds the `ygg-stage` tool itself.

## Layout
- `yggdrasil_slurm/`
  - `config.py` — object-storage config from env (the sourced `creds.env`)
  - `storage.py` — minimal boto3 S3 client (Garage-compatible)
  - `stage.py` → `ygg-stage pull <key> <dest>` / `ygg-stage push <dir> <prefix>`

## Install (once, on the cluster)
```bash
cd Yggdrasil/slurm
uv venv && uv pip install -e .        # provides ygg-stage
```
Make sure `ygg-stage` is on `PATH` for batch jobs (activate this venv in each
`algo/*/run.sbatch`, or install into the environment `uv run` uses there).

## Credentials & config
**Secrets are NOT stored here or anywhere on the cluster.** The runner worker writes a
0600 `creds.env` into each job's stage dir with `OBJECT_STORAGE_*`, `YGG_INPUT_KEYS`,
`YGG_OUTPUT_PREFIX`; the algo's `run.sbatch` sources it and a `trap` deletes it on exit.
