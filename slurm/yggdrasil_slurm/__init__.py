"""Cluster-side stage helper for Yggdrasil SLURM jobs.

Deployed to the cluster and installed once (``uv sync`` / ``uv pip install -e .``).
Provides a single console script, ``ygg-stage``, that moves data between object storage
and the cluster:

  ygg-stage pull <key> <dest>     download a job input
  ygg-stage push <dir> <prefix>   upload job outputs

Credentials are NOT stored on the cluster — ``ygg-stage`` reads them from the transient
per-job env file the runner worker drops (and the sbatch trap deletes). The cluster
never talks to the Yggdrasil API; the runner worker owns claim/complete/fail.
"""

__version__ = "0.1.0"
