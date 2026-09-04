"""Unit tests for the runner worker (common.runner) and the runner serializer.

All mocked — no DB, no SSH, no network — so they run as SimpleTestCase.
"""
from types import SimpleNamespace
import sys
from unittest import mock

from botocore.exceptions import EndpointConnectionError
from django.test import SimpleTestCase, override_settings

from common.object_storage import ObjectStorage, ObjectStorageError

from common.runner import run as run_mod
from common.runner.ssh import SlurmSSH, SlurmSSHError


class SshHelperTests(SimpleTestCase):
    def _ssh(self):
        return SlurmSSH(host="dummy", poll_interval=0, max_wall_seconds=10)

    def test_export_str_quotes_shell_assignments(self):
        s = SlurmSSH._export_str({"YGG_JOB_ID": 5, "YGG_STAGE": "/a b"})
        self.assertNotIn("ALL", s)
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
            output_path="/stage/logs/job_7-%j.out",
            error_path="/stage/logs/job_7-%j.err",
            work_dir="/stage",
        )
        self.assertEqual(sid, "900")
        self.assertIn("sbatch --parsable", captured["cmd"])
        self.assertIn("--output=/stage/logs/job_7-%j.out", captured["cmd"])
        self.assertIn("--error=/stage/logs/job_7-%j.err", captured["cmd"])
        self.assertIn("--chdir=/stage", captured["cmd"])
        self.assertTrue(captured["cmd"].startswith("YGG_JOB_ID=7 sbatch --parsable"))
        self.assertNotIn("--export", captured["cmd"])
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

    def test_poll_tolerates_the_submit_to_accounting_lag(self):
        ssh = SlurmSSH(host="d", poll_interval=5, max_wall_seconds=100,
                       unknown_grace_seconds=30)
        states = iter([None, None, "RUNNING", "COMPLETED"])
        ssh._state = lambda sid: next(states)
        with mock.patch("common.runner.ssh.time.sleep"):
            self.assertEqual(ssh.poll("900"), "COMPLETED")

    def test_poll_gives_up_on_an_id_accounting_never_knows(self):
        # A reattach to a purged allocation must not burn the 24h wall clock.
        ssh = SlurmSSH(host="d", poll_interval=5, max_wall_seconds=86400,
                       unknown_grace_seconds=10)
        ssh._state = lambda sid: None
        with mock.patch("common.runner.ssh.time.sleep"):
            with self.assertRaises(SlurmSSHError) as ctx:
                ssh.poll("900")
        self.assertIn("not visible in accounting", str(ctx.exception))

    def test_poll_unknown_grace_resets_once_the_job_appears(self):
        ssh = SlurmSSH(host="d", poll_interval=5, max_wall_seconds=1000,
                       unknown_grace_seconds=10)
        # Blips of invisibility between real states must not trip the bound.
        states = iter([None, "RUNNING", None, "RUNNING", None, "COMPLETED"])
        ssh._state = lambda sid: next(states)
        with mock.patch("common.runner.ssh.time.sleep"):
            self.assertEqual(ssh.poll("900"), "COMPLETED")

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
    def test_unreachable_store_surfaces_as_object_storage_error(self):
        """DNS/connection failure is one exception type, never raw botocore.

        CI has no object storage (garage:3900 does not resolve there), so every
        network call must normalize BotoCoreError into ObjectStorageError.
        file_access.exists() only swallows ObjectStorageError; a raw
        EndpointConnectionError escaping here is what failed CI run 91546356994.
        """
        store = ObjectStorage.__new__(ObjectStorage)
        store.bucket = "yggdrasil"
        store.key_prefix = ""
        client = mock.Mock()
        client.head_object.side_effect = EndpointConnectionError(
            endpoint_url="http://garage:3900/yggdrasil"
        )
        client.head_bucket.side_effect = EndpointConnectionError(
            endpoint_url="http://garage:3900/yggdrasil"
        )
        client.get_object.side_effect = EndpointConnectionError(
            endpoint_url="http://garage:3900/yggdrasil"
        )
        client.upload_file.side_effect = EndpointConnectionError(
            endpoint_url="http://garage:3900/yggdrasil"
        )
        store._client = client

        with self.assertRaises(ObjectStorageError):
            store.head("a.nii.gz")
        # exists() maps unreachable -> absent (False), same as missing key.
        self.assertFalse(store.exists("a.nii.gz"))
        with self.assertRaises(ObjectStorageError):
            store.get("a.nii.gz")
        with self.assertRaises(ObjectStorageError):
            store.ensure_bucket_exists()

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

    def test_normalize_cbct_output_adds_required_logical_name(self):
        path = "proj/processed/cbct/job_5/predictions/scan.nii.gz"
        out = run_mod._normalize_output_files(
            "cbct",
            {
                "predictions/scan.nii.gz": path,
                "gpu_stats.json": "proj/processed/cbct/job_5/gpu_stats.json",
            },
        )
        self.assertEqual(out["segmentation_nifti"], path)

    def test_normalize_cbct_output_rejects_ambiguous_nifti_files(self):
        output_files = {
            "predictions/a.nii.gz": "out/a.nii.gz",
            "predictions/b.nii.gz": "out/b.nii.gz",
        }
        self.assertEqual(
            run_mod._normalize_output_files("cbct", output_files), output_files
        )


    def test_video_derivatives_lose_their_container_extension(self):
        """`subsampled.mp4` on the bucket is `subtype='subsampled'` in the registry.

        Outputs are discovered by listing the prefix, so the key is a filename. The
        annotation gate and the export both look the sampled track up by its logical
        name, and a subtype of `subsampled.mp4` matches neither -- the annotator would
        stay closed on a study that had finished processing.
        """
        out = run_mod._normalize_output_files(
            "video",
            {
                "compressed.mp4": "lap/processed/video/job_9/compressed.mp4",
                "subsampled.mp4": "lap/processed/video/job_9/subsampled.mp4",
            },
        )

        self.assertEqual(
            out,
            {
                "compressed": "lap/processed/video/job_9/compressed.mp4",
                "subsampled": "lap/processed/video/job_9/subsampled.mp4",
            },
        )

    def test_a_video_derivative_already_named_logically_is_left_alone(self):
        output_files = {"subsampled": "lap/processed/video/job_9/subsampled.mp4"}

        self.assertEqual(run_mod._normalize_output_files("video", output_files), output_files)

    def test_an_unexpected_video_output_keeps_its_name(self):
        """Renaming is for the two derivatives the algorithm declares. Anything else is
        registered as it arrived rather than guessed at."""
        output_files = {"preview.png": "lap/processed/video/job_9/preview.png"}

        self.assertEqual(run_mod._normalize_output_files("video", output_files), output_files)


    def test_intraoral_outputs_lose_their_extension_too(self):
        """`mark_job_completed` indexes `output_files["segmentation_json"]`."""
        out = run_mod._normalize_output_files(
            "intraoral-photo",
            {
                "segmentation_json.json": "mx/processed/iop/job_3/segmentation_json.json",
                "views_json.json": "mx/processed/iop/job_3/views_json.json",
            },
        )

        self.assertEqual(
            sorted(out), ["segmentation_json", "views_json"]
        )

    def test_a_modality_with_no_logical_outputs_is_untouched(self):
        output_files = {"scan_upper.stl": "mx/processed/ios/job_1/scan_upper.stl"}

        self.assertEqual(run_mod._normalize_output_files("ios", output_files), output_files)


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
        with override_settings(SLURM_STAGE_DIR="/stage", ALGO_BASE_DIR="/algo"), \
             mock.patch.object(run_mod, "JobApiClient", return_value=api), \
             mock.patch.object(run_mod.SlurmSSH, "from_settings", return_value=ssh), \
             mock.patch.object(run_mod, "_collect_output_files", return_value={"a.stl": "k"}):
            result = run_mod.run_job(5)
        self.assertEqual(result, "completed")
        api.complete.assert_called_once()
        api.fail.assert_not_called()
        # creds file written 0600, then sbatch, then poll, then fallback cleanup
        ssh.mkdirs.assert_any_call("/stage/logs")
        ssh.sftp_write.assert_called_once()
        ssh.sbatch.assert_called_once()
        self.assertEqual(ssh.sbatch.call_args.kwargs["work_dir"], "/stage")
        self.assertEqual(
            ssh.sbatch.call_args.kwargs["output_path"], "/stage/logs/job_5-%j.out"
        )
        self.assertEqual(
            ssh.sbatch.call_args.kwargs["export"]["YGG_ALGO_DIR"], "/algo/sn"
        )
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
        with override_settings(SLURM_STAGE_DIR="/stage", ALGO_BASE_DIR="/algo"), \
             mock.patch.object(run_mod, "JobApiClient", return_value=api), \
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
        ssh.read_text_if_exists.assert_any_call("/stage/logs/job_5-900.out")
        ssh.read_text_if_exists.assert_any_call("/stage/logs/job_5-900.err")
        ssh.remove_file.assert_called_once()
        api.complete.assert_not_called()

    def test_completion_failure_reports_fail(self):
        api = mock.MagicMock()
        api.claim.return_value = {
            "algo_name": "U-Mamba2",
            "project_slug": "maxillo",
            "modality_slug": "cbct",
            "input_files": {"input": "maxillo/raw/cbct/scan.nii.gz"},
        }
        api.complete.side_effect = RuntimeError("invalid output contract")
        ssh = self._patch_ssh()
        with override_settings(SLURM_STAGE_DIR="/stage", ALGO_BASE_DIR="/algo"), \
             mock.patch.object(run_mod, "JobApiClient", return_value=api), \
             mock.patch.object(run_mod.SlurmSSH, "from_settings", return_value=ssh), \
             mock.patch.object(
                 run_mod,
                 "_collect_output_files",
                 return_value={"predictions/scan.nii.gz": "out/scan.nii.gz"},
             ):
            result = run_mod.run_job(5)

        self.assertEqual(result, "failed:completion")
        api.fail.assert_called_once_with(
            5, "Completion error: invalid output contract"
        )

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


class RunJobResumeTests(SimpleTestCase):
    """Recovery after a worker dies mid-allocation.

    The failure this closes: the runner container was recreated while blocked in
    ``poll``; the SLURM job went on to finish and push its outputs, and the job sat in
    ``processing`` forever because nothing recorded which allocation to look at.
    """

    def _ssh(self, poll_state="COMPLETED"):
        ssh = mock.MagicMock()
        ssh.__enter__.return_value = ssh
        ssh.__exit__.return_value = False
        ssh.sbatch.return_value = "900"
        ssh.poll.return_value = poll_state
        return ssh

    def _run(self, api, ssh, job_id=5):
        with override_settings(SLURM_STAGE_DIR="/stage", ALGO_BASE_DIR="/algo"), \
             mock.patch.object(run_mod, "JobApiClient", return_value=api), \
             mock.patch.object(run_mod.SlurmSSH, "from_settings", return_value=ssh), \
             mock.patch.object(run_mod, "_collect_output_files", return_value={"a": "k"}):
            return run_mod.run_job(job_id)

    def _claim(self, **extra):
        payload = {
            "algo_name": "sn", "project_slug": "maxillo",
            "modality_slug": "ios", "input_files": {},
        }
        payload.update(extra)
        return payload

    def test_allocation_is_stamped_before_the_wait_begins(self):
        # The window this closes is exactly "submitted but not yet recorded", so the
        # stamp has to land before the call that can block for 24h.
        api = mock.MagicMock()
        api.claim.return_value = self._claim()
        ssh = self._ssh()
        attached_before_poll = []
        ssh.poll.side_effect = lambda sid: (
            attached_before_poll.append(api.attach.called) or "COMPLETED"
        )

        self.assertEqual(self._run(api, ssh), "completed")

        api.attach.assert_called_once_with(5, "900")
        self.assertEqual(attached_before_poll, [True])

    def test_redelivered_task_reattaches_instead_of_resubmitting(self):
        api = mock.MagicMock()
        api.claim.return_value = self._claim(slurm_job_id="900")
        ssh = self._ssh()

        self.assertEqual(self._run(api, ssh), "completed")

        ssh.sbatch.assert_not_called()
        ssh.sftp_write.assert_not_called()
        ssh.poll.assert_called_once_with("900")
        api.complete.assert_called_once()
        api.attach.assert_not_called()

    def test_reattached_run_that_already_failed_is_reported(self):
        api = mock.MagicMock()
        api.claim.return_value = self._claim(slurm_job_id="900")
        ssh = self._ssh(poll_state="FAILED")
        ssh.accounting.return_value = {"state": "FAILED", "exit_code": "1:0"}
        ssh.read_text_if_exists.side_effect = ["out", "err"]

        self.assertEqual(self._run(api, ssh), "failed:FAILED")

        ssh.sbatch.assert_not_called()
        api.complete.assert_not_called()
        self.assertIn("SLURM job 900", api.fail.call_args.args[1])

    def test_blank_stamp_submits_normally(self):
        api = mock.MagicMock()
        api.claim.return_value = self._claim(slurm_job_id="  ")
        ssh = self._ssh()

        self.assertEqual(self._run(api, ssh), "completed")

        ssh.sbatch.assert_called_once()
        api.attach.assert_called_once_with(5, "900")


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
