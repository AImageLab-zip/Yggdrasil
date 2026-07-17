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
/work/yggdrasil_workers/Yggdrasil/.venv/bin/python -m pip install -e .
/work/yggdrasil_workers/Yggdrasil/.venv/bin/python -m pip install uv
```
This provides both `ygg-stage` and the `uv` executable from the shared Yggdrasil
venv, outside any user's home directory. Each `algo/*/run.sbatch` should make that
batch PATH explicit before calling either command:

```bash
export PATH=/work/yggdrasil_workers/Yggdrasil/.venv/bin:$PATH
```

The `uv` executable can live in the shared Yggdrasil venv while `uv run` still uses
each algorithm project's own environment from that algorithm directory.

Batch scripts should use the runner-provided `YGG_ALGO_DIR` as their algorithm
directory. Do not derive it from `BASH_SOURCE[0]`: SLURM executes a copied spool
script such as `/var/lib/slurm/slurmd/job.../slurm_script`, so that points outside
the deployed algorithm repo.

## Credentials & config
**Secrets are NOT stored here or anywhere on the cluster.** The runner worker writes a
0600 `creds.env` into each job's stage dir with `OBJECT_STORAGE_*`, `YGG_INPUT_KEYS`,
`YGG_OUTPUT_PREFIX`; it also exports `YGG_JOB_ID`, `YGG_STAGE`, `YGG_CREDS`, and
`YGG_ALGO_DIR` directly to `sbatch`. The algo's `run.sbatch` sources `YGG_CREDS` and
a `trap` deletes it on exit.
