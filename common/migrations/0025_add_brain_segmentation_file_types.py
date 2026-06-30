from django.db import migrations, models


def ensure_brain_segmentation_modality(apps, schema_editor):
    Project = apps.get_model('common', 'Project')
    Modality = apps.get_model('common', 'Modality')

    brain_project, _ = Project.objects.get_or_create(
        slug='brain',
        defaults={
            'name': 'Brain',
            'description': 'Brain tumor MRI project with T1, T2, FLAIR, T1c, and segmentation modalities',
            'icon': 'fas fa-brain',
            'is_active': True,
        },
    )

    modality, _ = Modality.objects.update_or_create(
        slug='braintumor-mri-seg',
        defaults={
            'name': 'Brain MRI Segmentation',
            'description': 'Brain Tumor Segmentation Mask',
            'icon': 'fas fa-brain',
            'label': 'SEG',
            'supported_extensions': ['.nii', '.nii.gz'],
            'requires_multiple_files': False,
            'is_active': True,
        },
    )
    brain_project.modalities.add(modality)


class Migration(migrations.Migration):

    dependencies = [
        ('common', '0024_collapse_project_roles'),
    ]

    operations = [
        migrations.RunPython(ensure_brain_segmentation_modality, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='fileregistry',
            name='file_type',
            field=models.CharField(
                choices=[
                    ('cbct_raw', 'CBCT Raw'),
                    ('cbct_processed', 'CBCT Processed'),
                    ('ios_raw_upper', 'IOS Raw Upper'),
                    ('ios_raw_lower', 'IOS Raw Lower'),
                    ('ios_processed_upper', 'IOS Processed Upper'),
                    ('ios_processed_lower', 'IOS Processed Lower'),
                    ('audio_raw', 'Audio Raw'),
                    ('audio_processed', 'Audio Processed Text'),
                    ('bite_classification', 'Bite Classification Results'),
                    ('rgb_image', 'RGB Image'),
                    ('volume_raw', 'Volume Raw'),
                    ('volume_processed', 'Volume Processed'),
                    ('image_raw', 'Image Raw'),
                    ('image_processed', 'Image Processed'),
                    ('generic_raw', 'Generic Raw'),
                    ('generic_processed', 'Generic Processed'),
                    ('braintumor_mri_t1_raw', 'Brain MRI T1 Raw'),
                    ('braintumor_mri_t1_processed', 'Brain MRI T1 Processed'),
                    ('braintumor_mri_t1c_raw', 'Brain MRI T1c Raw'),
                    ('braintumor_mri_t1c_processed', 'Brain MRI T1c Processed'),
                    ('braintumor_mri_t2_raw', 'Brain MRI T2 Raw'),
                    ('braintumor_mri_t2_processed', 'Brain MRI T2 Processed'),
                    ('braintumor_mri_flair_raw', 'Brain MRI FLAIR Raw'),
                    ('braintumor_mri_flair_processed', 'Brain MRI FLAIR Processed'),
                    ('braintumor_mri_seg_raw', 'Brain MRI Segmentation Raw'),
                    ('braintumor_mri_seg_processed', 'Brain MRI Segmentation Processed'),
                    ('intraoral_raw', 'Intraoral Photographs Raw'),
                    ('intraoral_processed', 'Intraoral Photographs Processed'),
                    ('teleradiography_raw', 'Teleradiography Raw'),
                    ('teleradiography_processed', 'Teleradiography Processed'),
                    ('panoramic_raw', 'panoramic Raw'),
                    ('panoramic_processed', 'panoramic Processed'),
                ],
                max_length=255,
            ),
        ),
    ]
