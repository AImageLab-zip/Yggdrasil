"""Orchestration for one Job: claim -> stage creds -> sbatch -> poll -> complete.

No job data flows through the worker. The worker only:
  - claims/completes the job over HTTP,
  - drops a transient 0600 creds file on the cluster,
  - submits the step's sbatch script and waits for it,
  - lists the produced output keys in object storage (it has the creds).
The sbatch job itself pulls inputs and pushes outputs (see Yggdrasil/slurm/scripts).
"""
import logging
import shlex

from django.conf import settings

from common.runner.job_api import ClaimError, JobApiClient
from common.runner.ssh import SlurmSSH

logger = logging.getLogger(__name__)

# Object-storage settings copied verbatim into the cluster creds file.
_STORAGE_KEYS = (
    "OBJECT_STORAGE_ENDPOINT_URL",
    "OBJECT_STORAGE_REGION",
    "OBJECT_STORAGE_ACCESS_KEY_ID",
    "OBJECT_STORAGE_SECRET_ACCESS_KEY",
    "OBJECT_STORAGE_BUCKET",
    "OBJECT_STORAGE_USE_SSL",
    "OBJECT_STORAGE_VERIFY_SSL",
    "OBJECT_STORAGE_ADDRESSING_STYLE",
    "OBJECT_STORAGE_KEY_PREFIX",
)


def iter_input_keys(input_files):
    """Yield every object-storage key (string leaf) in the input_files structure."""
    def walk(obj):
        if isinstance(obj, str):
            if obj:
                yield obj
        elif isinstance(obj, dict):
            for v in obj.values():
                yield from walk(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                yield from walk(v)

    seen = set()
    for key in walk(input_files or {}):
        if key not in seen:
            seen.add(key)
            yield key


def render_creds_env(input_keys, output_prefix):
    """Shell-sourceable env file for the cluster: storage creds + IO locations.

    Values are shell-quoted; the file is written 0600 into the job's private stage dir
    and deleted by the sbatch script's trap.
    """
    lines = []
    for name in _STORAGE_KEYS:
        val = getattr(settings, name, "")
        lines.append(f"export {name}={shlex.quote(str(val))}")
    lines.append(f"export YGG_INPUT_KEYS={shlex.quote(' '.join(input_keys))}")
    lines.append(f"export YGG_OUTPUT_PREFIX={shlex.quote(output_prefix)}")
    return "\n".join(lines) + "\n"


def _collect_output_files(output_prefix):
    """List the produced objects under the output prefix; map relpath -> key."""
    from common.object_storage import get_object_storage

    storage = get_object_storage()
    output_files = {}
    prefix = output_prefix.rstrip("/") + "/"
    for key in storage.list_keys(prefix):
        rel = key[len(prefix):] if key.startswith(prefix) else key
        if rel:
            output_files[rel] = key
    return output_files


def run_job(job_id: int) -> str:
    """Entry point for the Celery task. Returns a short status string."""
    api = JobApiClient()
    try:
        job = api.claim(job_id)
    except ClaimError as exc:
        logger.info("Skipping job %s: %s", job_id, exc)
        return "skipped"

    algo_name = (job.get("algo_name") or "").strip()
    if not algo_name:
        api.fail(job_id, "No algo_name configured for this step")
        return "failed:no-script"

    project = job.get("project_slug") or "maxillo"
    modality = job.get("modality_slug") or "output"
    output_prefix = f"{project}/processed/{modality}/job_{job_id}"
    input_keys = list(iter_input_keys(job.get("input_files")))

    stage_base = getattr(settings, "SLURM_STAGE_DIR", "").rstrip("/")
    algo_base = getattr(settings, "ALGO_BASE_DIR", "").rstrip("/")
    stage = f"{stage_base}/job_{job_id}"
    creds_path = f"{stage}/creds.env"
    script_path = f"{algo_base}/{algo_name}/run.sbatch"

    try:
        with SlurmSSH.from_settings() as ssh:
            ssh.mkdirs(f"{stage}/in")
            ssh.mkdirs(f"{stage}/out")
            ssh.sftp_write(creds_path, render_creds_env(input_keys, output_prefix))
            slurm_id = ssh.sbatch(
                script_path=script_path,
                export={
                    "YGG_JOB_ID": job_id,
                    "YGG_STAGE": stage,
                    "YGG_CREDS": creds_path,
                },
            )
            logger.info("Job %s submitted as SLURM %s; waiting", job_id, slurm_id)
            state = ssh.poll(slurm_id)
    except Exception as exc:
        logger.exception("Runner failed for job %s", job_id)
        api.fail(job_id, f"Runner error: {exc}")
        return "failed:runner"

    if state != "COMPLETED":
        api.fail(job_id, f"SLURM job {slurm_id} ended in state {state}")
        return f"failed:{state}"

    output_files = _collect_output_files(output_prefix)
    api.complete(job_id, output_files=output_files, logs="")
    logger.info("Job %s completed (slurm %s, %d outputs)", job_id, slurm_id, len(output_files))
    return "completed"
