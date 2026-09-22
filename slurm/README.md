# Yggdrasil SLURM stage helper (cluster side)

Cluster-side counterpart to Yggdrasil 2.0's runner worker. The **runner worker** (a
Celery worker in the Yggdrasil deploy) claims a job, SSHes in, drops a transient
per-job `creds.env`, and `sbatch`es `ALGO_BASE_DIR/<algo_name>/run.sbatch`. That script
uses `ygg-stage` (this package) to pull inputs and push outputs **directly** between
object storage and the cluster — no data flows through the worker, and by default the
job holds only presigned grants for its own inputs and outputs (see below).

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
The runner worker writes a transient `creds.env` into each job's stage dir (the dir is
`0700`, the file is `0600` before any byte is written) and exports `YGG_JOB_ID`,
`YGG_STAGE`, `YGG_CREDS` and `YGG_ALGO_DIR` to `sbatch`. The algo's `run.sbatch`
sources `YGG_CREDS`, and a `trap` deletes it on exit. What the file holds depends on
the deployment's `RUNNER_STAGE_MODE`:

- **`presigned`** (default): `YGG_STAGE_MODE=presigned` and `YGG_STAGE_GRANTS`, a JSON
  document with one presigned GET URL per input key and a POST policy that accepts
  uploads only under this job's output prefix. **No storage credentials reach the
  cluster**: a job can read its own inputs and write its own outputs, nothing else,
  and only until the grants expire (`RUNNER_PRESIGN_TTL_SECONDS`, at most 7 days).
- **`credentials`** (legacy, for a cluster still on `ygg-stage` 0.1): the deployment's
  `OBJECT_STORAGE_*` keys, which can read and write the **entire bucket** -- every
  patient of every project. Use only while upgrading.

Both modes also carry `YGG_INPUT_KEYS` and `YGG_OUTPUT_PREFIX`. `ygg-stage pull/push`
behave identically in both, so `run.sbatch` scripts need no change.

## Upgrading to 0.2 (presigned grants)
`ygg-stage` 0.2 understands both modes, so upgrade the cluster **before or together
with** deploying a web app whose `RUNNER_STAGE_MODE` is `presigned`:
```bash
cd /work/yggdrasil_workers/Yggdrasil && git pull
/work/yggdrasil_workers/Yggdrasil/.venv/bin/python -m pip install -e slurm
/work/yggdrasil_workers/Yggdrasil/.venv/bin/ygg-stage --help   # sanity check
```
If jobs must run before the cluster can be upgraded, set `RUNNER_STAGE_MODE=credentials`
in the deployment's `.env` and restart the runner worker; remove it once upgraded.
