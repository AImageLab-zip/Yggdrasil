"""Data migration: say which domain each Modality belongs to.

Before 0048 a modality's domain existed only as three imperative seeder
commands (``create_maxillo_modalities``, ``setup_brain_modalities``,
``setup_laparoscopy_modalities``) and, after they had run, as the set of
projects the modality happened to be attached to. Neither is a fact the admin
can consult while rendering an *empty* add form, which is what makes it
possible to offer bite classification on a brain project.

MODALITY_DOMAINS is those three seeder lists, transcribed. Anything not in it
falls back to the domains of the projects the modality is already attached to
-- and stays blank when that is empty or ambiguous, because blank means
"available everywhere" and only ever *widens* what the admin offers. A wrong
narrowing would hide a modality a project legitimately collects; a wrong
widening is the status quo.
"""

from django.db import migrations

# slug -> domain. The three per-domain seeder commands, transcribed.
MODALITY_DOMAINS = {
    # maxillo/management/commands/create_maxillo_modalities.py
    "cbct": "maxillo",
    "ios": "maxillo",
    "intraoral-photo": "maxillo",
    "teleradiography": "maxillo",
    "panoramic": "maxillo",
    "rawzip": "maxillo",
    # brain/management/commands/setup_brain_modalities.py
    "braintumor-mri-t1": "brain",
    "braintumor-mri-t1c": "brain",
    "braintumor-mri-t2": "brain",
    "braintumor-mri-flair": "brain",
    "braintumor-mri-seg": "brain",
    # laparoscopy/management/commands/setup_laparoscopy_modalities.py
    "video": "laparoscopy",
}


def forwards(apps, schema_editor):
    Modality = apps.get_model("common", "Modality")

    for modality in Modality.objects.all():
        domain = MODALITY_DOMAINS.get(modality.slug)
        if domain is None:
            # Not one of the seeded modalities. Infer from the projects it is
            # already attached to, and only when they agree on one domain.
            domains = set(
                modality.projects.values_list("domain", flat=True).distinct()
            )
            domains.discard("")
            domain = domains.pop() if len(domains) == 1 else ""
        if modality.domain != domain:
            modality.domain = domain
            modality.save(update_fields=["domain"])


def backwards(apps, schema_editor):
    apps.get_model("common", "Modality").objects.update(domain="")


class Migration(migrations.Migration):

    dependencies = [
        ("common", "0048_modality_domain"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
