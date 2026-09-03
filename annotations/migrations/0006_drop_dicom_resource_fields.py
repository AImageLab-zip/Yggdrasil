"""Drop the DICOM-only identity fields and the DICOM choices.

``SourceResource.frame_of_reference_uid`` stays: it is written for NIfTI/Cornerstone
volumes too (``annotations.adapters.cornerstone``), and coordinates in
``patient_lps_mm`` are only comparable within one frame of reference regardless of
where the bytes came from. Only ``sop_instance_uid`` and ``series_instance_uid`` --
which nothing but the deleted DICOM ingest ever wrote -- are removed, together with
the index over the latter.

The two ``AlterField``s carry no data change; they align the stored ``choices=`` with
``annotations.constants`` so ``makemigrations --check`` stays clean.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("annotations", "0005_convert_legacy_annotations"),
        # The two DICOM models are deleted in the same release; ordering the two
        # migrations relative to each other keeps a fresh `migrate` deterministic.
        ("common", "0052_delete_dicom_catalog"),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name="sourceresource",
            name="annotations_series__feaecf_idx",
        ),
        migrations.RemoveField(model_name="sourceresource", name="sop_instance_uid"),
        migrations.RemoveField(model_name="sourceresource", name="series_instance_uid"),
        migrations.AlterField(
            model_name="sourceresource",
            name="kind",
            field=models.CharField(
                choices=[
                    ("file", "File"),
                    ("derived_resource", "Derived resource"),
                    ("logical_volume", "Logical volume"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="annotationpayload",
            name="format",
            field=models.CharField(
                choices=[
                    ("yggdrasil_json", "Yggdrasil JSON"),
                    ("cornerstone_state", "Cornerstone state"),
                    ("nifti_labelmap", "NIfTI labelmap"),
                    ("png_render", "PNG render"),
                    ("npz_mask", "NPZ mask"),
                ],
                max_length=32,
            ),
        ),
        # Help-text only, for the same reason: the wording named DICOM.
        migrations.AlterField(
            model_name="measurementitem",
            name="calibration_note",
            field=models.CharField(
                blank=True,
                help_text="Where the scale came from: a NIfTI affine, a manual ruler.",
                max_length=255,
            ),
        ),
        migrations.AlterField(
            model_name="sourceresource",
            name="file",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Set for kind=file and for bundle members; NULL for derived kinds."
                ),
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="annotation_resources",
                to="common.fileregistry",
            ),
        ),
    ]
