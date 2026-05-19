from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('brain', '0008_export_sharing_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='FolderAccess',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('role', models.CharField(choices=[('standard', 'Standard User'), ('annotator', 'Annotator'), ('project_manager', 'Project Manager')], default='standard', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('folder', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='access_list', to='brain.folder')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='brain_folder_access', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'brain_folder_access',
                'indexes': [models.Index(fields=['folder'], name='brain_folde_folder__7178b6_idx'), models.Index(fields=['user'], name='brain_folde_user_id_84dd07_idx'), models.Index(fields=['role'], name='brain_folde_role_7a5169_idx'), models.Index(fields=['folder', 'role'], name='brain_folde_folder__b3167d_idx'), models.Index(fields=['user', 'role'], name='brain_folde_user_id_15ceef_idx')],
                'unique_together': {('user', 'folder')},
            },
        ),
    ]
