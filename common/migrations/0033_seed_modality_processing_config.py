from django.conf import settings
from django.db import migrations

# Historical hardcoded list from maxillo/file_utils.py: image modalities that
# do not need runner processing (their upload Job is born 'completed').
LEGACY_NO_PROCESSING = {"panoramic", "teleradiography", "rawzip"}

# Historical hardcoded dependency wired imperatively at IOS upload time.
LEGACY_DEPENDENCIES = {"bite_classification": ["ios"]}


def _env_enabled_for(slug, queue_by_modality):
    """Reproduce is_runner_enabled_for_modality at migrate time.

    Empty/absent map => everything enabled; otherwise a modality is enabled
    only if it has a non-blank queue entry.
    """
    if not isinstance(queue_by_modality, dict) or not queue_by_modality:
        return True
    queue = queue_by_modality.get(slug)
    return isinstance(queue, str) and bool(queue.strip())


def seed_forward(apps, schema_editor):
    Modality = apps.get_model('common', 'Modality')
    ModalityProcessingConfig = apps.get_model('common', 'ModalityProcessingConfig')

    queue_by_modality = getattr(settings, 'RUNNER_QUEUE_BY_MODALITY', None) or {}
    if not isinstance(queue_by_modality, dict):
        queue_by_modality = {}

    by_slug = {}
    for modality in Modality.objects.all():
        slug = modality.slug
        requires_processing = slug not in LEGACY_NO_PROCESSING
        config, _ = ModalityProcessingConfig.objects.update_or_create(
            modality=modality,
            defaults={
                'requires_processing': requires_processing,
                'is_enabled': _env_enabled_for(slug, queue_by_modality),
                'queue_name': (queue_by_modality.get(slug) or '') if isinstance(queue_by_modality.get(slug), str) else '',
                'is_blocking': requires_processing,
            },
        )
        by_slug[slug] = (modality, config)

    # Seed the historical ios -> bite_classification dependency.
    for dependent_slug, dep_slugs in LEGACY_DEPENDENCIES.items():
        entry = by_slug.get(dependent_slug)
        if not entry:
            continue
        _, config = entry
        for dep_slug in dep_slugs:
            dep_entry = by_slug.get(dep_slug)
            if dep_entry:
                config.depends_on.add(dep_entry[0])


def seed_reverse(apps, schema_editor):
    ModalityProcessingConfig = apps.get_model('common', 'ModalityProcessingConfig')
    ModalityProcessingConfig.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('common', '0032_modalityprocessingconfig'),
    ]

    operations = [
        migrations.RunPython(seed_forward, reverse_code=seed_reverse),
    ]
