from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("common", "0026_add_intraoral_photo_processed_type"),
        ("common", "0024_fileregistry_laparoscopy_patient_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="fileregistry",
            name="domain",
            field=models.CharField(
                choices=[
                    ("maxillo", "Maxillo"),
                    ("brain", "Brain"),
                    ("laparoscopy", "Laparoscopy"),
                ],
                default="maxillo",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="fileregistry",
            name="file_type",
            field=models.CharField(
                choices=[
                    ("cbct_raw", "CBCT Raw"),
                    ("cbct_processed", "CBCT Processed"),
                    ("ios_raw_upper", "IOS Raw Upper"),
                    ("ios_raw_lower", "IOS Raw Lower"),
                    ("ios_processed_upper", "IOS Processed Upper"),
                    ("ios_processed_lower", "IOS Processed Lower"),
                    ("audio_raw", "Audio Raw"),
                    ("audio_processed", "Audio Processed Text"),
                    ("bite_classification", "Bite Classification Results"),
                    ("rgb_image", "RGB Image"),
                    ("volume_raw", "Volume Raw"),
                    ("volume_processed", "Volume Processed"),
                    ("image_raw", "Image Raw"),
                    ("image_processed", "Image Processed"),
                    ("generic_raw", "Generic Raw"),
                    ("generic_processed", "Generic Processed"),
                    ("braintumor_mri_t1_raw", "Brain MRI T1 Raw"),
                    ("braintumor_mri_t1_processed", "Brain MRI T1 Processed"),
                    ("braintumor_mri_t1c_raw", "Brain MRI T1c Raw"),
                    ("braintumor_mri_t1c_processed", "Brain MRI T1c Processed"),
                    ("braintumor_mri_t2_raw", "Brain MRI T2 Raw"),
                    ("braintumor_mri_t2_processed", "Brain MRI T2 Processed"),
                    ("braintumor_mri_flair_raw", "Brain MRI FLAIR Raw"),
                    ("braintumor_mri_flair_processed", "Brain MRI FLAIR Processed"),
                    ("intraoral_raw", "Intraoral Photographs Raw"),
                    ("intraoral_processed", "Intraoral Photographs Processed"),
                    ("intraoral-photo_processed", "Intraoral Photo Processed"),
                    ("teleradiography_raw", "Teleradiography Raw"),
                    ("teleradiography_processed", "Teleradiography Processed"),
                    ("panoramic_raw", "panoramic Raw"),
                    ("panoramic_processed", "panoramic Processed"),
                    ("video_raw", "Video Raw"),
                    ("video_processed", "Video Processed"),
                ],
                max_length=255,
            ),
        ),
        migrations.AlterField(
            model_name="job",
            name="domain",
            field=models.CharField(
                choices=[
                    ("maxillo", "Maxillo"),
                    ("brain", "Brain"),
                    ("laparoscopy", "Laparoscopy"),
                ],
                default="maxillo",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="processingjob",
            name="domain",
            field=models.CharField(
                choices=[
                    ("maxillo", "Maxillo"),
                    ("brain", "Brain"),
                    ("laparoscopy", "Laparoscopy"),
                ],
                default="maxillo",
                max_length=20,
            ),
        ),
    ]
