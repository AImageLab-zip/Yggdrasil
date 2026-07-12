"""Unit tests for the runner worker (common.runner) and the runner serializer.

All mocked — no DB, no SSH, no network — so they run as SimpleTestCase.
"""
from types import SimpleNamespace
import sys
from unittest import mock

from django.test import SimpleTestCase

from common.runner import run as run_mod
from common.runner.ssh import SlurmSSH, SlurmSSHError


class SshHelperTests(SimpleTestCase):
    def _ssh(self):
        return SlurmSSH(host="dummy", poll_interval=0, max_wall_seconds=10)

    def test_export_str_quotes_and_prefixes_all(self):
        s = SlurmSSH._export_str({"YGG_JOB_ID": 5, "YGG_STAGE": "/a b"})
        self.assertTrue(s.startswith("ALL,"))
        self.assertIn("YGG_JOB_ID=5", s)
        self.assertIn("YGG_STAGE='/a b'", s)  # shell-quoted

    def test_sbatch_builds_command_and_parses_id(self):
        ssh = self._ssh()
        captured = {}

        def fake_run(cmd, timeout=120):
            captured["cmd"] = cmd
            return 0, "900;cluster\n", ""

        ssh.run = fake_run
        sid = ssh.sbatch(
            script_path="/algo/sn/run.sbatch",
            export={"YGG_JOB_ID": 7},
            output_path="/stage/job_7/slurm-%j.out",
            error_path="/stage/job_7/slurm-%j.err",
        )
        self.assertEqual(sid, "900")
        self.assertIn("sbatch --parsable", captured["cmd"])
        self.assertIn("--output=/stage/job_7/slurm-%j.out", captured["cmd"])
        self.assertIn("--error=/stage/job_7/slurm-%j.err", captured["cmd"])
        self.assertIn("--export=ALL,YGG_JOB_ID=7", captured["cmd"])
        self.assertIn("/algo/sn/run.sbatch", captured["cmd"])

    def test_sbatch_raises_on_failure(self):
        ssh = self._ssh()
        ssh.run = lambda cmd, timeout=120: (1, "", "boom")
        with self.assertRaises(SlurmSSHError):
            ssh.sbatch(script_path="/x", export={})

    def test_poll_waits_until_terminal(self):
        ssh = self._ssh()
        states = iter(["PENDING", "RUNNING", "COMPLETED"])
        ssh._state = lambda sid: next(states)
        with mock.patch("common.runner.ssh.time.sleep"):
            self.assertEqual(ssh.poll("900"), "COMPLETED")

    def test_state_uses_wide_column_and_normalizes_truncation(self):
        ssh = self._ssh()
        captured = {}

        def fake_run(cmd, timeout=120):
            captured["cmd"] = cmd
            return 0, "CANCELLED+ \n", ""

        ssh.run = fake_run
        self.assertEqual(ssh._state("900"), "CANCELLED")
        self.assertIn("State%32", captured["cmd"])

    def test_state_normalizes_accounting_annotation(self):
        ssh = self._ssh()
        ssh.run = lambda cmd, timeout=120: (0, "CANCELLED by 0\n", "")
        self.assertEqual(ssh._state("900"), "CANCELLED")

    def test_accounting_preserves_full_state(self):
        ssh = self._ssh()
        ssh.run = lambda cmd, timeout=120: (
            0,
            "CANCELLED by 0|0:0|None|00:00:02|germano|sbatch --parsable /x\n",
            "",
        )

        details = ssh.accounting("900")

        self.assertEqual(details["state"], "CANCELLED by 0")
        self.assertEqual(details["base_state"], "CANCELLED")
        self.assertEqual(details["exit_code"], "0:0")
        self.assertEqual(details["node_list"], "germano")
        self.assertEqual(details["submit_line"], "sbatch --parsable /x")

    def test_poll_times_out(self):
        ssh = SlurmSSH(host="d", poll_interval=1, max_wall_seconds=1)
        ssh._state = lambda sid: "RUNNING"
        with mock.patch("common.runner.ssh.time.sleep"):
            with self.assertRaises(SlurmSSHError):
                ssh.poll("900")

    def test_connect_passes_key_and_password(self):
        client = mock.MagicMock()
        paramiko = SimpleNamespace(
            SSHClient=mock.MagicMock(return_value=client),
            RejectPolicy=mock.MagicMock(return_value="reject-policy"),
        )
        ssh = SlurmSSH(
            host="login.example",
            port=2222,
            user="yggdrasil",
            key_path="/run/secrets/slurm_ssh_key",
            password="secret",
        )

        with mock.patch.dict(sys.modules, {"paramiko": paramiko}):
            with ssh:
                pass

        client.connect.assert_called_once_with(
            hostname="login.example",
            port=2222,
            username="yggdrasil",
            key_filename="/run/secrets/slurm_ssh_key",
            password="secret",
            timeout=30,
            allow_agent=True,
            look_for_keys=True,
        )


class RunHelperTests(SimpleTestCase):
    def test_iter_input_keys_nested_dedup(self):
        keys = list(run_mod.iter_input_keys(
            {"ios": {"upper": "p/u.stl", "lower": "p/l.stl"}, "flat": "p/f.stl", "dup": "p/u.stl"}
        ))
        self.assertEqual(set(keys), {"p/u.stl", "p/l.stl", "p/f.stl"})
        self.assertEqual(len(keys), 3)  # de-duplicated

    def test_render_creds_env_has_storage_and_io(self):
        body = run_mod.render_creds_env(["p/a.stl", "p/b.stl"], "proj/processed/ios/job_5")
        self.assertIn("export OBJECT_STORAGE_ENDPOINT_URL=", body)
        self.assertIn("export OBJECT_STORAGE_SECRET_ACCESS_KEY=", body)
        self.assertIn("export YGG_INPUT_KEYS='p/a.stl p/b.stl'", body)  # quoted (space)
        # No shell metachars -> shlex.quote leaves it bare; assert the value is present.
        self.assertIn("export YGG_OUTPUT_PREFIX=proj/processed/ios/job_5", body)

    def test_collect_output_files_strips_prefix(self):
        storage = SimpleNamespace(
            list_keys=lambda prefix: [
                "proj/processed/ios/job_5/a_oriented.stl",
                "proj/processed/ios/job_5/sub/b.stl",
            ]
        )
        with mock.patch("common.object_storage.get_object_storage", return_value=storage):
            out = run_mod._collect_output_files("proj/processed/ios/job_5")
        self.assertEqual(
            out,
            {
                "a_oriented.stl": "proj/processed/ios/job_5/a_oriented.stl",
                "sub/b.stl": "proj/processed/ios/job_5/sub/b.stl",
            },
        )


class RunJobTests(SimpleTestCase):
    def _patch_ssh(self, poll_state="COMPLETED"):
        ssh = mock.MagicMock()
        ssh.__enter__.return_value = ssh
        ssh.__exit__.return_value = False
        ssh.sbatch.return_value = "900"
        ssh.poll.return_value = poll_state
        return ssh

    def test_happy_path_completes(self):
        api = mock.MagicMock()
        api.claim.return_value = {
            "algo_name": "sn",
            "project_slug": "maxillo", "modality_slug": "ios",
            "input_files": {"ios": "maxillo/raw/ios/a.stl"},
        }
        ssh = self._patch_ssh()
        with mock.patch.object(run_mod, "JobApiClient", return_value=api), \
             mock.patch.object(run_mod.SlurmSSH, "from_settings", return_value=ssh), \
             mock.patch.object(run_mod, "_collect_output_files", return_value={"a.stl": "k"}):
            result = run_mod.run_job(5)
        self.assertEqual(result, "completed")
        api.complete.assert_called_once()
        api.fail.assert_not_called()
        # creds file written 0600, then sbatch, then poll, then fallback cleanup
        ssh.sftp_write.assert_called_once()
        ssh.sbatch.assert_called_once()
        ssh.remove_file.assert_called_once()
        self.assertTrue(
            ssh.sbatch.call_args.kwargs["script_path"].endswith("/sn/run.sbatch")
        )

    def test_failed_slurm_state_reports_fail(self):
        api = mock.MagicMock()
        api.claim.return_value = {
            "algo_name": "sn", "project_slug": "maxillo",
            "modality_slug": "ios", "input_files": {},
        }
        ssh = self._patch_ssh(poll_state="FAILED")
        ssh.accounting.return_value = {
            "state": "FAILED",
            "exit_code": "1:0",
            "reason": "None",
            "elapsed": "00:00:03",
            "node_list": "node-a",
            "submit_line": "sbatch --parsable /algo/sn/run.sbatch",
        }
        ssh.read_text_if_exists.side_effect = ["stdout text", "stderr text"]
        with mock.patch.object(run_mod, "JobApiClient", return_value=api), \
             mock.patch.object(run_mod.SlurmSSH, "from_settings", return_value=ssh):
            result = run_mod.run_job(5)
        self.assertEqual(result, "failed:FAILED")
        api.fail.assert_called_once()
        error = api.fail.call_args.args[1]
        self.assertIn("SLURM job 900 ended in state FAILED", error)
        self.assertIn("exit_code: 1:0", error)
        self.assertIn("node_list: node-a", error)
        self.assertIn("stdout text", error)
        self.assertIn("stderr text", error)
        ssh.remove_file.assert_called_once()
        api.complete.assert_not_called()

    def test_missing_script_fails_fast(self):
        api = mock.MagicMock()
        api.claim.return_value = {"algo_name": "", "input_files": {}}
        with mock.patch.object(run_mod, "JobApiClient", return_value=api):
            result = run_mod.run_job(5)
        self.assertEqual(result, "failed:no-script")
        api.fail.assert_called_once()

    def test_unclaimable_is_skipped(self):
        from common.runner.job_api import ClaimError

        api = mock.MagicMock()
        api.claim.side_effect = ClaimError("already running")
        with mock.patch.object(run_mod, "JobApiClient", return_value=api):
            result = run_mod.run_job(5)
        self.assertEqual(result, "skipped")


class SerializerTests(SimpleTestCase):
    def test_step_dispatch_config_from_step(self):
        from maxillo.runner_api_service import _step_dispatch_config

        job = SimpleNamespace(modality_slug="ios_orientation")
        step = SimpleNamespace(algo_name="sn")
        with mock.patch("common.modality_config.get_step", return_value=step):
            cfg = _step_dispatch_config(job)
        self.assertEqual(cfg, {"algo_name": "sn"})

    def test_step_dispatch_config_no_step(self):
        from maxillo.runner_api_service import _step_dispatch_config

        job = SimpleNamespace(modality_slug="panoramic")
        with mock.patch("common.modality_config.get_step", return_value=None):
            cfg = _step_dispatch_config(job)
        self.assertEqual(cfg, {"algo_name": ""})
