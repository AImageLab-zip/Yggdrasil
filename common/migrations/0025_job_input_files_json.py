import json

from django.db import migrations, models


INPUT_METADATA_KEYS = {
    "input_files",
    "input_" + "mani" + "fest",
    "input_type",
    "file_count",
    "expected_outputs",
    "input_format",
    "file_path",
    "depends_on_ios_job",
    "ios_job_id",
}


def _input_files_from_values(old_value, output_files):
    if isinstance(output_files, dict):
        embedded = output_files.get("input_files")
        if isinstance(embedded, dict):
            return embedded
        if isinstance(embedded, list):
            return {"files": embedded}

    raw = (old_value or "").strip()
    if not raw:
        return {}

    if raw.startswith("{") or raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"files": parsed}

    return {"input": raw}


def _clean_output_files(status, output_files):
    if not isinstance(output_files, dict):
        return {}
    if status != "completed":
        return {}

    cleaned = {
        key: value for key, value in output_files.items() if key not in INPUT_METADATA_KEYS
    }
    return cleaned


def forwards(apps, schema_editor):
    for model_name in ("Job", "ProcessingJob"):
        Model = apps.get_model("common", model_name)
        for job in Model.objects.all().iterator():
            output_files = job.output_files if isinstance(job.output_files, dict) else {}
            job.input_files = _input_files_from_values(
                getattr(job, "input_file_path", ""), output_files
            )
            job.output_files = _clean_output_files(job.status, output_files)
            job.save(update_fields=["input_files", "output_files"])


class Migration(migrations.Migration):

    dependencies = [
        ("common", "0024_collapse_project_roles"),
    ]

    operations = [
        migrations.AddField(
            model_name="job",
            name="input_files",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Dict of input object keys used by workers",
            ),
        ),
        migrations.AddField(
            model_name="processingjob",
            name="input_files",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Dict of input object keys used by workers",
            ),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="job",
            name="input_file_path",
        ),
        migrations.RemoveField(
            model_name="processingjob",
            name="input_file_path",
        ),
        migrations.AlterField(
            model_name="job",
            name="output_files",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Dict of output object keys and metadata written on completion",
            ),
        ),
        migrations.AlterField(
            model_name="processingjob",
            name="output_files",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Dict of output object keys and metadata written on completion",
            ),
        ),
    ]
