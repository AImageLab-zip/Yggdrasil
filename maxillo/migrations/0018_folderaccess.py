from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('maxillo', '0017_intraoral_segmentation_confirmation'),
    ]

    operations = [
        migrations.CreateModel(
            name='FolderAccess',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('role', models.CharField(choices=[('standard', 'Standard User'), ('annotator', 'Annotator'), ('project_manager', 'Project Manager')], default='standard', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('folder', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='access_list', to='maxillo.folder')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='maxillo_folder_access', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'indexes': [models.Index(fields=['folder'], name='maxillo_fol_folder__e922d8_idx'), models.Index(fields=['user'], name='maxillo_fol_user_id_3f7f89_idx'), models.Index(fields=['role'], name='maxillo_fol_role_275cb5_idx'), models.Index(fields=['folder', 'role'], name='maxillo_fol_folder__68c0eb_idx'), models.Index(fields=['user', 'role'], name='maxillo_fol_user_id_1801c8_idx')],
                'unique_together': {('user', 'folder')},
            },
        ),
    ]
