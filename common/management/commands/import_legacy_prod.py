import json
import re

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


class Command(BaseCommand):
    help = 'Import legacy scans_* production dump (in a temp DB) into strict maxillo/brain schema.'

    def add_arguments(self, parser):
        parser.add_argument('--legacy-db', default='legacy_prod_20260331', help='Legacy DB name containing scans_* tables')
        parser.add_argument('--dry-run', action='store_true', help='Only print source counts, no writes')

    def handle(self, *args, **options):
        legacy_db = options['legacy_db']
        dry_run = options['dry_run']

        if not re.match(r'^[A-Za-z0-9_]+$', legacy_db):
            raise CommandError('Invalid legacy DB name')

        with connection.cursor() as cursor:
            cursor.execute(f"SHOW TABLES FROM `{legacy_db}` LIKE 'scans_patient'")
            if not cursor.fetchone():
                raise CommandError(f'Legacy database {legacy_db} is missing scans_* tables')

            cursor.execute(f"SELECT id, slug FROM `{legacy_db}`.`common_project`")
            project_rows = cursor.fetchall()
            project_by_slug = {slug: pid for pid, slug in project_rows}

            maxillo_project_id = project_by_slug.get('maxillo')
            brain_project_id = project_by_slug.get('brain')
            if not maxillo_project_id or not brain_project_id:
                raise CommandError('Legacy common_project must contain maxillo and brain slugs')

            if dry_run:
                self._print_counts(cursor, legacy_db, maxillo_project_id, brain_project_id)
                self.stdout.write(self.style.SUCCESS('Dry-run completed.'))
                return

            with transaction.atomic():
                self._truncate_target(cursor)
                self._import_users(cursor, legacy_db)
                self._import_projects_and_modalities(cursor, legacy_db)
                self._import_project_access(cursor, legacy_db)

                self._import_domain_core(cursor, legacy_db, maxillo_project_id, 'maxillo')
                self._import_domain_core(cursor, legacy_db, brain_project_id, 'brain')

                self._import_jobs(cursor, legacy_db, maxillo_project_id, brain_project_id)
                self._import_processing_jobs(cursor, legacy_db, maxillo_project_id, brain_project_id)
                self._import_file_registry(cursor, legacy_db, maxillo_project_id, brain_project_id)
                self._import_dependencies(cursor, legacy_db)
                self._import_exports(cursor, legacy_db, maxillo_project_id, brain_project_id)

            self._print_target_counts()
            self.stdout.write(self.style.SUCCESS('Legacy import completed successfully.'))

    def _truncate_target(self, cursor):
        tables = [
            'maxillo_job_dependencies',
            'maxillo_processingjob_dependencies',
            'maxillo_fileregistry',
            'maxillo_processingjob',
            'maxillo_job',
            'brain_patient_tags',
            'brain_patient_modalities',
            'maxillo_patient_tags',
            'maxillo_patient_modalities',
            'brain_classification',
            'brain_voicecaption',
            'brain_export',
            'brain_patient',
            'brain_tag',
            'brain_folder',
            'brain_dataset',
            'maxillo_classification',
            'maxillo_voicecaption',
            'maxillo_export',
            'maxillo_patient',
            'maxillo_tag',
            'maxillo_folder',
            'maxillo_dataset',
            'common_projectaccess',
            'common_project_modalities',
            'common_project',
            'common_modality',
            'auth_user_user_permissions',
            'auth_user_groups',
            'auth_user',
        ]

        cursor.execute('SET FOREIGN_KEY_CHECKS = 0')
        for table in tables:
            cursor.execute(f'TRUNCATE TABLE `{table}`')
        cursor.execute('SET FOREIGN_KEY_CHECKS = 1')

    def _import_users(self, cursor, legacy_db):
        cursor.execute(
            f"""
            INSERT INTO `auth_user`
            (`id`, `password`, `last_login`, `is_superuser`, `username`, `first_name`, `last_name`, `email`, `is_staff`, `is_active`, `date_joined`)
            SELECT
                `id`, `password`, `last_login`, `is_superuser`, `username`, `first_name`, `last_name`, `email`, `is_staff`, `is_active`, `date_joined`
            FROM `{legacy_db}`.`auth_user`
            """
        )

    def _import_projects_and_modalities(self, cursor, legacy_db):
        cursor.execute(
            f"""
            INSERT INTO `common_project`
            (`id`, `name`, `slug`, `description`, `icon`, `is_active`, `created_at`, `created_by_id`)
            SELECT `id`, `name`, `slug`, `description`, `icon`, `is_active`, `created_at`, `created_by_id`
            FROM `{legacy_db}`.`common_project`
            """
        )
        cursor.execute(
            f"""
            INSERT INTO `common_modality`
            (`id`, `name`, `slug`, `description`, `supported_extensions`, `requires_multiple_files`, `is_active`, `created_at`, `created_by_id`, `icon`, `subtypes`, `label`)
            SELECT `id`, `name`, `slug`, `description`, `supported_extensions`, `requires_multiple_files`, `is_active`, `created_at`, `created_by_id`, `icon`, `subtypes`, `label`
            FROM `{legacy_db}`.`common_modality`
            """
        )
        cursor.execute(
            f"""
            INSERT INTO `common_project_modalities` (`project_id`, `modality_id`)
            SELECT `project_id`, `modality_id`
            FROM `{legacy_db}`.`common_project_modalities`
            """
        )

    def _import_project_access(self, cursor, legacy_db):
        cursor.execute(
            f"""
            INSERT INTO `common_projectaccess` (`user_id`, `project_id`, `role`, `created_at`)
            SELECT
                pa.`user_id`,
                pa.`project_id`,
                CASE
                    WHEN up.`role` IN ('standard', 'annotator', 'project_manager', 'admin', 'student_dev') THEN up.`role`
                    WHEN up.`role` = 'demo' THEN 'standard'
                    ELSE 'standard'
                END AS `role`,
                pa.`created_at`
            FROM `{legacy_db}`.`common_projectaccess` pa
            LEFT JOIN `{legacy_db}`.`scans_userprofile` up ON up.`user_id` = pa.`user_id`
            """
        )

    def _import_domain_core(self, cursor, legacy_db, project_id, domain):
        app = 'brain' if domain == 'brain' else 'maxillo'

        cursor.execute(
            f"""
            INSERT INTO `{app}_dataset` (`id`, `name`, `description`, `created_at`, `created_by_id`)
            SELECT DISTINCT d.`id`, d.`name`, d.`description`, d.`created_at`, d.`created_by_id`
            FROM `{legacy_db}`.`scans_dataset` d
            JOIN `{legacy_db}`.`scans_patient` p ON p.`dataset_id` = d.`id`
            WHERE p.`project_id` = %s
            """,
            [project_id],
        )

        cursor.execute(
            f"""
            INSERT INTO `{app}_folder` (`id`, `name`, `created_at`, `created_by_id`, `parent_id`)
            SELECT DISTINCT f.`id`, f.`name`, f.`created_at`, f.`created_by_id`, f.`parent_id`
            FROM `{legacy_db}`.`scans_folder` f
            JOIN `{legacy_db}`.`scans_patient` p ON p.`folder_id` = f.`id`
            WHERE p.`project_id` = %s
            """,
            [project_id],
        )

        cursor.execute(
            f"""
            INSERT INTO `{app}_tag` (`id`, `name`, `created_at`)
            SELECT DISTINCT t.`id`, t.`name`, t.`created_at`
            FROM `{legacy_db}`.`scans_tag` t
            JOIN `{legacy_db}`.`scans_patient_tags` pt ON pt.`tag_id` = t.`id`
            JOIN `{legacy_db}`.`scans_patient` p ON p.`patient_id` = pt.`patient_id`
            WHERE p.`project_id` = %s
            """,
            [project_id],
        )

        cursor.execute(
            f"""
            INSERT INTO `{app}_patient`
            (`patient_id`, `name`, `upper_scan_raw`, `lower_scan_raw`, `upper_scan_norm`, `lower_scan_norm`, `cbct`, `ios_processing_status`, `cbct_processing_status`, `visibility`, `uploaded_at`, `dataset_id`, `folder_id`, `uploaded_by_id`, `deleted`)
            SELECT
                `patient_id`, `name`, `upper_scan_raw`, `lower_scan_raw`, `upper_scan_norm`, `lower_scan_norm`, `cbct`, `ios_processing_status`, `cbct_processing_status`, `visibility`, `uploaded_at`, `dataset_id`, `folder_id`, `uploaded_by_id`, 0
            FROM `{legacy_db}`.`scans_patient`
            WHERE `project_id` = %s
            """,
            [project_id],
        )

        cursor.execute(
            f"""
            INSERT INTO `{app}_patient_tags` (`patient_id`, `tag_id`)
            SELECT pt.`patient_id`, pt.`tag_id`
            FROM `{legacy_db}`.`scans_patient_tags` pt
            JOIN `{legacy_db}`.`scans_patient` p ON p.`patient_id` = pt.`patient_id`
            WHERE p.`project_id` = %s
            """,
            [project_id],
        )

        cursor.execute(
            f"""
            INSERT INTO `{app}_patient_modalities` (`patient_id`, `modality_id`)
            SELECT pm.`patient_id`, pm.`modality_id`
            FROM `{legacy_db}`.`scans_patient_modalities` pm
            JOIN `{legacy_db}`.`scans_patient` p ON p.`patient_id` = pm.`patient_id`
            WHERE p.`project_id` = %s
            """,
            [project_id],
        )

        cursor.execute(
            f"""
            INSERT INTO `{app}_classification`
            (`id`, `classifier`, `sagittal_left`, `sagittal_right`, `vertical`, `transverse`, `midline`, `timestamp`, `annotator_id`, `patient_id`)
            SELECT
                c.`id`, c.`classifier`, c.`sagittal_left`, c.`sagittal_right`, c.`vertical`, c.`transverse`, c.`midline`, c.`timestamp`, c.`annotator_id`, c.`patient_id`
            FROM `{legacy_db}`.`scans_classification` c
            JOIN `{legacy_db}`.`scans_patient` p ON p.`patient_id` = c.`patient_id`
            WHERE p.`project_id` = %s
            """,
            [project_id],
        )

        cursor.execute(
            f"""
            INSERT INTO `{app}_voicecaption`
            (`id`, `modality`, `duration`, `text_caption`, `original_text_caption`, `is_edited`, `edit_history`, `processing_status`, `created_at`, `updated_at`, `patient_id`, `user_id`)
            SELECT
                v.`id`, v.`modality`, v.`duration`, v.`text_caption`, v.`original_text_caption`, v.`is_edited`, v.`edit_history`, v.`processing_status`, v.`created_at`, v.`updated_at`, v.`patient_id`, v.`user_id`
            FROM `{legacy_db}`.`scans_voicecaption` v
            JOIN `{legacy_db}`.`scans_patient` p ON p.`patient_id` = v.`patient_id`
            WHERE p.`project_id` = %s
            """,
            [project_id],
        )

    def _import_jobs(self, cursor, legacy_db, maxillo_project_id, brain_project_id):
        cursor.execute(
            f"""
            INSERT INTO `maxillo_job`
            (`id`, `modality_slug`, `status`, `priority`, `input_files`, `output_files`, `created_at`, `started_at`, `completed_at`, `retry_count`, `max_retries`, `error_logs`, `worker_id`, `patient_id`, `voice_caption_id`, `brain_patient_id`, `brain_voice_caption_id`, `domain`)
            SELECT
                j.`id`, j.`modality_slug`, j.`status`, j.`priority`, JSON_OBJECT('input', j.`input_file_path`), j.`output_files`, j.`created_at`, j.`started_at`, j.`completed_at`, j.`retry_count`, j.`max_retries`, j.`error_logs`, j.`worker_id`,
                j.`patient_id`, j.`voice_caption_id`, NULL, NULL, 'maxillo'
            FROM `{legacy_db}`.`scans_job` j
            LEFT JOIN `{legacy_db}`.`scans_patient` p ON p.`patient_id` = j.`patient_id`
            LEFT JOIN `{legacy_db}`.`scans_voicecaption` v ON v.`id` = j.`voice_caption_id`
            LEFT JOIN `{legacy_db}`.`scans_patient` vp ON vp.`patient_id` = v.`patient_id`
            WHERE COALESCE(p.`project_id`, vp.`project_id`, %s) = %s
            """,
            [maxillo_project_id, maxillo_project_id],
        )

        cursor.execute(
            f"""
            INSERT INTO `maxillo_job`
            (`id`, `modality_slug`, `status`, `priority`, `input_files`, `output_files`, `created_at`, `started_at`, `completed_at`, `retry_count`, `max_retries`, `error_logs`, `worker_id`, `patient_id`, `voice_caption_id`, `brain_patient_id`, `brain_voice_caption_id`, `domain`)
            SELECT
                j.`id`, j.`modality_slug`, j.`status`, j.`priority`, JSON_OBJECT('input', j.`input_file_path`), j.`output_files`, j.`created_at`, j.`started_at`, j.`completed_at`, j.`retry_count`, j.`max_retries`, j.`error_logs`, j.`worker_id`,
                NULL, NULL, j.`patient_id`, j.`voice_caption_id`, 'brain'
            FROM `{legacy_db}`.`scans_job` j
            LEFT JOIN `{legacy_db}`.`scans_patient` p ON p.`patient_id` = j.`patient_id`
            LEFT JOIN `{legacy_db}`.`scans_voicecaption` v ON v.`id` = j.`voice_caption_id`
            LEFT JOIN `{legacy_db}`.`scans_patient` vp ON vp.`patient_id` = v.`patient_id`
            WHERE COALESCE(p.`project_id`, vp.`project_id`) = %s
            """,
            [brain_project_id],
        )

    def _import_processing_jobs(self, cursor, legacy_db, maxillo_project_id, brain_project_id):
        cursor.execute(
            f"""
            INSERT INTO `maxillo_processingjob`
            (`id`, `job_type`, `status`, `priority`, `input_files`, `output_files`, `docker_image`, `docker_command`, `created_at`, `started_at`, `completed_at`, `retry_count`, `max_retries`, `error_logs`, `worker_id`, `patient_id`, `voice_caption_id`, `brain_patient_id`, `brain_voice_caption_id`, `domain`)
            SELECT
                j.`id`, j.`job_type`, j.`status`, j.`priority`, JSON_OBJECT('input', j.`input_file_path`), j.`output_files`, j.`docker_image`, j.`docker_command`, j.`created_at`, j.`started_at`, j.`completed_at`, j.`retry_count`, j.`max_retries`, j.`error_logs`, j.`worker_id`,
                j.`patient_id`, j.`voice_caption_id`, NULL, NULL, 'maxillo'
            FROM `{legacy_db}`.`scans_processingjob` j
            LEFT JOIN `{legacy_db}`.`scans_patient` p ON p.`patient_id` = j.`patient_id`
            LEFT JOIN `{legacy_db}`.`scans_voicecaption` v ON v.`id` = j.`voice_caption_id`
            LEFT JOIN `{legacy_db}`.`scans_patient` vp ON vp.`patient_id` = v.`patient_id`
            WHERE COALESCE(p.`project_id`, vp.`project_id`, %s) = %s
            """,
            [maxillo_project_id, maxillo_project_id],
        )

        cursor.execute(
            f"""
            INSERT INTO `maxillo_processingjob`
            (`id`, `job_type`, `status`, `priority`, `input_files`, `output_files`, `docker_image`, `docker_command`, `created_at`, `started_at`, `completed_at`, `retry_count`, `max_retries`, `error_logs`, `worker_id`, `patient_id`, `voice_caption_id`, `brain_patient_id`, `brain_voice_caption_id`, `domain`)
            SELECT
                j.`id`, j.`job_type`, j.`status`, j.`priority`, JSON_OBJECT('input', j.`input_file_path`), j.`output_files`, j.`docker_image`, j.`docker_command`, j.`created_at`, j.`started_at`, j.`completed_at`, j.`retry_count`, j.`max_retries`, j.`error_logs`, j.`worker_id`,
                NULL, NULL, j.`patient_id`, j.`voice_caption_id`, 'brain'
            FROM `{legacy_db}`.`scans_processingjob` j
            LEFT JOIN `{legacy_db}`.`scans_patient` p ON p.`patient_id` = j.`patient_id`
            LEFT JOIN `{legacy_db}`.`scans_voicecaption` v ON v.`id` = j.`voice_caption_id`
            LEFT JOIN `{legacy_db}`.`scans_patient` vp ON vp.`patient_id` = v.`patient_id`
            WHERE COALESCE(p.`project_id`, vp.`project_id`) = %s
            """,
            [brain_project_id],
        )

    def _import_file_registry(self, cursor, legacy_db, maxillo_project_id, brain_project_id):
        cursor.execute(
            f"""
            INSERT INTO `maxillo_fileregistry`
            (`id`, `file_type`, `file_path`, `file_size`, `file_hash`, `created_at`, `metadata`, `patient_id`, `voice_caption_id`, `processing_job_id`, `modality_id`, `subtype`, `brain_patient_id`, `brain_voice_caption_id`, `domain`)
            SELECT
                f.`id`, f.`file_type`, f.`file_path`, f.`file_size`, f.`file_hash`, f.`created_at`, f.`metadata`,
                f.`patient_id`, f.`voice_caption_id`, f.`processing_job_id`, f.`modality_id`, f.`subtype`, NULL, NULL, 'maxillo'
            FROM `{legacy_db}`.`scans_fileregistry` f
            LEFT JOIN `{legacy_db}`.`scans_patient` p ON p.`patient_id` = f.`patient_id`
            LEFT JOIN `{legacy_db}`.`scans_voicecaption` v ON v.`id` = f.`voice_caption_id`
            LEFT JOIN `{legacy_db}`.`scans_patient` vp ON vp.`patient_id` = v.`patient_id`
            WHERE COALESCE(p.`project_id`, vp.`project_id`, %s) = %s
            """,
            [maxillo_project_id, maxillo_project_id],
        )

        cursor.execute(
            f"""
            INSERT INTO `maxillo_fileregistry`
            (`id`, `file_type`, `file_path`, `file_size`, `file_hash`, `created_at`, `metadata`, `patient_id`, `voice_caption_id`, `processing_job_id`, `modality_id`, `subtype`, `brain_patient_id`, `brain_voice_caption_id`, `domain`)
            SELECT
                f.`id`, f.`file_type`, f.`file_path`, f.`file_size`, f.`file_hash`, f.`created_at`, f.`metadata`,
                NULL, NULL, f.`processing_job_id`, f.`modality_id`, f.`subtype`, f.`patient_id`, f.`voice_caption_id`, 'brain'
            FROM `{legacy_db}`.`scans_fileregistry` f
            LEFT JOIN `{legacy_db}`.`scans_patient` p ON p.`patient_id` = f.`patient_id`
            LEFT JOIN `{legacy_db}`.`scans_voicecaption` v ON v.`id` = f.`voice_caption_id`
            LEFT JOIN `{legacy_db}`.`scans_patient` vp ON vp.`patient_id` = v.`patient_id`
            WHERE COALESCE(p.`project_id`, vp.`project_id`) = %s
            """,
            [brain_project_id],
        )

    def _import_dependencies(self, cursor, legacy_db):
        cursor.execute(
            f"""
            INSERT INTO `maxillo_job_dependencies` (`id`, `from_job_id`, `to_job_id`)
            SELECT `id`, `from_job_id`, `to_job_id`
            FROM `{legacy_db}`.`scans_job_dependencies`
            """
        )
        cursor.execute(
            f"""
            INSERT INTO `maxillo_processingjob_dependencies` (`id`, `from_processingjob_id`, `to_processingjob_id`)
            SELECT `id`, `from_processingjob_id`, `to_processingjob_id`
            FROM `{legacy_db}`.`scans_processingjob_dependencies`
            """
        )

    def _import_exports(self, cursor, legacy_db, maxillo_project_id, brain_project_id):
        cursor.execute(
            f"""
            SELECT
                `id`, `status`, `query_params`, `query_summary`, `file_path`, `file_size`, `patient_count`,
                `created_at`, `started_at`, `completed_at`, `error_message`, `user_id`, `progress_message`, `progress_percent`
            FROM `{legacy_db}`.`scans_export`
            ORDER BY `id`
            """
        )
        rows = cursor.fetchall()

        cursor.execute(f"SELECT `patient_id` FROM `{legacy_db}`.`scans_patient` WHERE `project_id` = %s", [brain_project_id])
        brain_patient_ids = {row[0] for row in cursor.fetchall()}
        cursor.execute(f"SELECT `patient_id` FROM `{legacy_db}`.`scans_patient` WHERE `project_id` = %s", [maxillo_project_id])
        maxillo_patient_ids = {row[0] for row in cursor.fetchall()}

        cursor.execute(f"SELECT DISTINCT `folder_id` FROM `{legacy_db}`.`scans_patient` WHERE `project_id` = %s AND `folder_id` IS NOT NULL", [brain_project_id])
        brain_folder_ids = {row[0] for row in cursor.fetchall()}
        cursor.execute(f"SELECT DISTINCT `folder_id` FROM `{legacy_db}`.`scans_patient` WHERE `project_id` = %s AND `folder_id` IS NOT NULL", [maxillo_project_id])
        maxillo_folder_ids = {row[0] for row in cursor.fetchall()}

        maxillo_values = []
        brain_values = []
        for row in rows:
            params = row[2]
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except Exception:
                    params = {}
            if not isinstance(params, dict):
                params = {}

            domain = self._resolve_export_domain(
                params,
                row[3] or '',
                brain_patient_ids,
                maxillo_patient_ids,
                brain_folder_ids,
                maxillo_folder_ids,
            )

            if domain == 'brain':
                brain_values.append(row)
            else:
                maxillo_values.append(row)

        if maxillo_values:
            cursor.executemany(
                """
                INSERT INTO `maxillo_export`
                (`id`, `status`, `query_params`, `query_summary`, `file_path`, `file_size`, `patient_count`, `created_at`, `started_at`, `completed_at`, `error_message`, `user_id`, `progress_message`, `progress_percent`)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                maxillo_values,
            )

        if brain_values:
            cursor.executemany(
                """
                INSERT INTO `brain_export`
                (`id`, `status`, `query_params`, `query_summary`, `file_path`, `file_size`, `patient_count`, `created_at`, `started_at`, `completed_at`, `error_message`, `user_id`, `progress_message`, `progress_percent`)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                brain_values,
            )

    def _resolve_export_domain(self, params, query_summary, brain_patient_ids, maxillo_patient_ids, brain_folder_ids, maxillo_folder_ids):
        project_slug = str(params.get('project_slug', '')).strip().lower()
        if project_slug in ['brain', 'maxillo']:
            return project_slug

        patient_ids = params.get('patient_ids') or []
        if isinstance(patient_ids, list):
            if any(pid in brain_patient_ids for pid in patient_ids):
                return 'brain'
            if any(pid in maxillo_patient_ids for pid in patient_ids):
                return 'maxillo'

        folder_ids = params.get('folder_ids') or []
        if isinstance(folder_ids, list):
            if any(fid in brain_folder_ids for fid in folder_ids):
                return 'brain'
            if any(fid in maxillo_folder_ids for fid in folder_ids):
                return 'maxillo'

        if 'brain' in query_summary.lower():
            return 'brain'
        return 'maxillo'

    def _print_counts(self, cursor, legacy_db, maxillo_project_id, brain_project_id):
        self.stdout.write('Legacy source counts:')
        cursor.execute(f"SELECT COUNT(*) FROM `{legacy_db}`.`auth_user`")
        self.stdout.write(f"- users: {cursor.fetchone()[0]}")

        cursor.execute(f"SELECT COUNT(*) FROM `{legacy_db}`.`scans_patient` WHERE `project_id`=%s", [maxillo_project_id])
        self.stdout.write(f"- maxillo patients: {cursor.fetchone()[0]}")
        cursor.execute(f"SELECT COUNT(*) FROM `{legacy_db}`.`scans_patient` WHERE `project_id`=%s", [brain_project_id])
        self.stdout.write(f"- brain patients: {cursor.fetchone()[0]}")

        cursor.execute(f"SELECT COUNT(*) FROM `{legacy_db}`.`scans_job`")
        self.stdout.write(f"- jobs: {cursor.fetchone()[0]}")
        cursor.execute(f"SELECT COUNT(*) FROM `{legacy_db}`.`scans_fileregistry`")
        self.stdout.write(f"- files: {cursor.fetchone()[0]}")
        cursor.execute(f"SELECT COUNT(*) FROM `{legacy_db}`.`scans_export`")
        self.stdout.write(f"- exports: {cursor.fetchone()[0]}")

    def _print_target_counts(self):
        with connection.cursor() as cursor:
            self.stdout.write('Imported target counts:')
            for label, table in [
                ('users', 'auth_user'),
                ('maxillo_patients', 'maxillo_patient'),
                ('brain_patients', 'brain_patient'),
                ('jobs', 'maxillo_job'),
                ('files', 'maxillo_fileregistry'),
                ('maxillo_exports', 'maxillo_export'),
                ('brain_exports', 'brain_export'),
            ]:
                cursor.execute(f"SELECT COUNT(*) FROM `{table}`")
                self.stdout.write(f"- {label}: {cursor.fetchone()[0]}")
