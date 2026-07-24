"""Thin SSH/SLURM helper for the runner worker (paramiko).

Opens one connection per job, stages a private creds file over SFTP, submits the step's
sbatch script, and polls ``sacct`` until the job reaches a terminal state. No SLURM
logic runs anywhere else — the web app never imports this module.
"""
import logging
import shlex
import time

from django.conf import settings

logger = logging.getLogger(__name__)

# SLURM terminal (final) states — anything else means "still going".
_TERMINAL_STATES = {
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY",
    "NODE_FAIL", "BOOT_FAIL", "DEADLINE", "PREEMPTED", "REVOKED",
}


class SlurmSSHError(RuntimeError):
    pass


class SlurmSSH:
    def __init__(self, *, host, port=22, user=None, key_path=None,
                 password=None, poll_interval=15, max_wall_seconds=24 * 3600,
                 connect_timeout=30):
        if not host:
            raise SlurmSSHError("SLURM_SSH_HOST is not configured")
        self.host = host
        self.port = int(port or 22)
        self.user = user or None
        self.key_path = key_path or None
        self.password = password or None
        self.poll_interval = int(poll_interval)
        self.max_wall_seconds = int(max_wall_seconds)
        self.connect_timeout = connect_timeout
        self._client = None

    @classmethod
    def from_settings(cls):
        return cls(
            host=getattr(settings, "SLURM_SSH_HOST", ""),
            port=getattr(settings, "SLURM_SSH_PORT", 22),
            user=getattr(settings, "SLURM_SSH_USER", "") or None,
            key_path=getattr(settings, "SLURM_SSH_KEY", "") or None,
            password=getattr(settings, "SLURM_SSH_PASSWORD", "") or None,
            poll_interval=getattr(settings, "SLURM_POLL_INTERVAL", 15),
            max_wall_seconds=getattr(settings, "SLURM_MAX_WALL_SECONDS", 24 * 3600),
        )

    # -- connection --------------------------------------------------------

    def __enter__(self):
        import paramiko

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(
            hostname=self.host,
            port=self.port,
            username=self.user,
            key_filename=self.key_path,
            password=self.password,
            timeout=self.connect_timeout,
            allow_agent=True,
            look_for_keys=True,
        )
        self._client = client
        return self

    def __exit__(self, *exc):
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- primitives --------------------------------------------------------

    def run(self, cmd, timeout=120):
        """Run a command; return (exit_code, stdout, stderr)."""
        stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        return code, out, err

    def mkdirs(self, path):
        code, _out, err = self.run(f"mkdir -p {shlex.quote(path)}")
        if code != 0:
            raise SlurmSSHError(f"mkdir -p {path} failed: {err.strip()}")

    def sftp_write(self, path, data, mode=0o600):
        sftp = self._client.open_sftp()
        try:
            with sftp.file(path, "w") as f:
                f.write(data)
            sftp.chmod(path, mode)
        finally:
            sftp.close()

    # -- slurm -------------------------------------------------------------

    @staticmethod
    def _export_str(export):
        return " ".join(
            f"{k}={shlex.quote(str(v))}" for k, v in export.items()
        )

    def sbatch(
        self, *, script_path, export, output_path=None, error_path=None, work_dir=None
    ):
        """Submit the script; return the SLURM job id (via --parsable).

        No resource flags are passed here — partition/gres/time/etc are
        #SBATCH directives baked into each algo's own run.sbatch.
        """
        # This cluster rejects explicit sbatch --export options. Prefixing the
        # submit command preserves SLURM's default environment propagation.
        cmd = []
        if export:
            cmd.append(self._export_str(export))
        cmd.extend(["sbatch", "--parsable"])
        if output_path:
            cmd.append(f"--output={shlex.quote(output_path)}")
        if error_path:
            cmd.append(f"--error={shlex.quote(error_path)}")
        if work_dir:
            cmd.append(f"--chdir={shlex.quote(work_dir)}")
        cmd.append(shlex.quote(script_path))
        code, out, err = self.run(" ".join(cmd))
        if code != 0:
            raise SlurmSSHError(f"sbatch failed: {err.strip() or out.strip()}")
        return out.strip().split(";")[0]

    def accounting(self, slurm_id):
        """Return useful sacct fields for a completed allocation."""
        fields = (
            "State%32,ExitCode,Reason%80,Elapsed,NodeList%80,SubmitLine%200"
        )
        code, out, _err = self.run(
            f"sacct -j {shlex.quote(str(slurm_id))} -X -P -n -o {fields}",
            timeout=60,
        )
        if code != 0 or not out.strip():
            return {}

        parts = out.strip().splitlines()[0].split("|", 5)
        while len(parts) < 6:
            parts.append("")
        state, exit_code, reason, elapsed, node_list, submit_line = parts
        return {
            "state": state.strip(),
            "base_state": self._normalize_state(state),
            "exit_code": exit_code.strip(),
            "reason": reason.strip(),
            "elapsed": elapsed.strip(),
            "node_list": node_list.strip(),
            "submit_line": submit_line.strip(),
        }

    def read_text_if_exists(self, path, max_bytes=12000):
        """Read a small text file from the cluster, or return empty string."""
        quoted = shlex.quote(path)
        code, out, _err = self.run(
            f"test -f {quoted} && python3 -c "
            f"'import pathlib, sys; "
            f"p=pathlib.Path(sys.argv[1]); "
            f"sys.stdout.buffer.write(p.read_bytes()[-{int(max_bytes)}:])' {quoted}",
            timeout=60,
        )
        if code != 0:
            return ""
        return out

    def remove_file(self, path):
        """Best-effort removal for transient files created by the runner."""
        code, _out, err = self.run(f"rm -f {shlex.quote(path)}", timeout=60)
        if code != 0:
            logger.warning("rm -f %s failed: %s", path, err.strip())

    def _state(self, slurm_id):
        # -X = job allocation only (no steps); -n = no header.
        code, out, _err = self.run(
            f"sacct -j {shlex.quote(str(slurm_id))} -X -n -o State%32", timeout=60
        )
        if code != 0 or not out.strip():
            return None  # not yet visible in accounting; treat as still-going
        return self._normalize_state(out.strip().splitlines()[0])

    @staticmethod
    def _normalize_state(state):
        """Return SLURM's base state, without accounting annotations/truncation."""
        return state.strip().split()[0].rstrip("+")

    def poll(self, slurm_id):
        """Block until the job reaches a terminal state; return that state."""
        waited = 0
        while True:
            state = self._state(slurm_id)
            if state and state in _TERMINAL_STATES:
                return state
            if waited >= self.max_wall_seconds:
                raise SlurmSSHError(
                    f"slurm job {slurm_id} did not finish within "
                    f"{self.max_wall_seconds}s (last state {state})"
                )
            time.sleep(self.poll_interval)
            waited += self.poll_interval
