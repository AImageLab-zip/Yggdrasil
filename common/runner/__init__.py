"""Runner worker: the only component that talks to the SLURM cluster.

A dedicated Celery worker (see docker-compose ``runner-worker``) consumes the runner
queues and runs ``common.runner.run.run_job`` for each Job. It holds the object-storage
credentials and the SSH key; it claims/completes jobs over the HTTP runner API, and it
injects credentials into the cluster transiently (per job) so the sbatch job moves its
own data — no bytes ever pass through the worker. The web app knows none of this.
"""
