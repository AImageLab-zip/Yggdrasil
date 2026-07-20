from django.db import migrations, models


def create_site_maintenance(apps, schema_editor):
    SiteMaintenance = apps.get_model("common", "SiteMaintenance")
    SiteMaintenance.objects.get_or_create(pk=1)


class Migration(migrations.Migration):

    dependencies = [
        ("common", "0038_merge_20260717_0000"),
    ]

    operations = [
        migrations.CreateModel(
            name="SiteMaintenance",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ("access_mode", models.CharField(choices=[("normal", "Normal"), ("read_only", "Read-only"), ("lockdown", "Full lockdown")], default="normal", max_length=20)),
                ("planned_message_enabled", models.BooleanField(default=False)),
                ("planned_message", models.TextField(blank=True, max_length=1000)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "site maintenance",
                "verbose_name_plural": "site maintenance",
            },
        ),
        migrations.AddConstraint(
            model_name="sitemaintenance",
            constraint=models.CheckConstraint(condition=models.Q(("id", 1)), name="common_site_maintenance_singleton"),
        ),
        migrations.RunPython(create_site_maintenance, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="fileregistry",
            name="file_type",
            field=models.CharField(choices=[("cbct_raw", "CBCT Raw"), ("cbct_processed", "CBCT Processed"), ("ios_raw_upper", "IOS Raw Upper"), ("ios_raw_lower", "IOS Raw Lower"), ("ios_processed_upper", "IOS Processed Upper"), ("ios_processed_lower", "IOS Processed Lower"), ("ios_landmarks", "IOS Landmarks"), ("ios_landmarks_prediction", "IOS Landmark Prediction"), ("audio_raw", "Audio Raw"), ("audio_processed", "Audio Processed Text"), ("bite_classification", "Bite Classification Results"), ("rgb_image", "RGB Image"), ("volume_raw", "Volume Raw"), ("volume_processed", "Volume Processed"), ("image_raw", "Image Raw"), ("image_processed", "Image Processed"), ("generic_raw", "Generic Raw"), ("generic_processed", "Generic Processed"), ("braintumor_mri_t1_raw", "Brain MRI T1 Raw"), ("braintumor_mri_t1_processed", "Brain MRI T1 Processed"), ("braintumor_mri_t1c_raw", "Brain MRI T1c Raw"), ("braintumor_mri_t1c_processed", "Brain MRI T1c Processed"), ("braintumor_mri_t2_raw", "Brain MRI T2 Raw"), ("braintumor_mri_t2_processed", "Brain MRI T2 Processed"), ("braintumor_mri_flair_raw", "Brain MRI FLAIR Raw"), ("braintumor_mri_flair_processed", "Brain MRI FLAIR Processed"), ("braintumor_mri_seg_raw", "Brain MRI Segmentation Raw"), ("braintumor_mri_seg_processed", "Brain MRI Segmentation Processed"), ("intraoral_raw", "Intraoral Photographs Raw"), ("intraoral_processed", "Intraoral Photographs Processed"), ("intraoral-photo_processed", "Intraoral Photo Processed"), ("teleradiography_raw", "Teleradiography Raw"), ("teleradiography_processed", "Teleradiography Processed"), ("panoramic_raw", "panoramic Raw"), ("panoramic_processed", "panoramic Processed"), ("video_raw", "Video Raw"), ("video_processed", "Video Processed")], max_length=255),
        ),
    ]
