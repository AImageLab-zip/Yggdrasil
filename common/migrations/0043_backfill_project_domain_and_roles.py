"""Data migration: backfill Project.domain, map legacy roles to viewer/
annotator/admin, and seed the AnnotationMethod registry.

Depends on 0042 (schema). The per-domain folder->project data migrations
(one per app) run after this and rely on AnnotationMethod rows existing here.
"""

from django.db import migrations

DOMAIN_SLUGS = {"maxillo", "brain", "laparoscopy"}

ROLE_MAP = {"standard": "viewer"}  # admin stays admin

ANNOTATION_METHODS = [
    # (slug, name, domain, icon)
    ("ios_landmarks", "IOS Landmarks", "maxillo", "fas fa-location-dot"),
    ("bite_classification", "Bite Classification", "maxillo", "fas fa-clipboard-check"),
    ("intraoral_segmentation", "Intraoral Segmentation", "maxillo", "fas fa-draw-polygon"),
    ("classification", "Cephalometric Classification", "maxillo", "fas fa-sliders"),
    ("voice_caption", "Voice Captions", "", "fas fa-microphone"),
    ("video_regions", "Video Region Annotation", "laparoscopy", "fas fa-shapes"),
    ("mri_caption", "MRI Captioning", "brain", "fas fa-comment-dots"),
]


def forwards(apps, schema_editor):
    Project = apps.get_model("common", "Project")
    ProjectAccess = apps.get_model("common", "ProjectAccess")
    Invitation = apps.get_model("common", "Invitation")
    AnnotationMethod = apps.get_model("common", "AnnotationMethod")

    # 1. Backfill Project.domain from slug (existing rows are domain-scoped).
    for project in Project.objects.all():
        domain = project.slug if project.slug in DOMAIN_SLUGS else "maxillo"
        if not project.domain or project.domain != domain:
            project.domain = domain
            project.save(update_fields=["domain"])

    # 2. Map legacy roles.
    ProjectAccess.objects.filter(role="standard").update(role="viewer")
    Invitation.objects.filter(role="standard").update(role="viewer")

    # 3. Seed the AnnotationMethod registry and wire each domain project to its
    #    applicable methods (its own domain's methods + the common ones).
    methods = {}
    for slug, name, domain, icon in ANNOTATION_METHODS:
        method, _ = AnnotationMethod.objects.get_or_create(
            slug=slug, defaults={"name": name, "domain": domain, "icon": icon}
        )
        if method.name != name or method.domain != domain or method.icon != icon:
            method.name = name
            method.domain = domain
            method.icon = icon
            method.save(update_fields=["name", "domain", "icon"])
        methods[slug] = method

    common_methods = AnnotationMethod.objects.filter(domain="")
    for project in Project.objects.all():
        domain_methods = AnnotationMethod.objects.filter(domain=project.domain)
        project.annotation_methods.set(domain_methods | common_methods)


class Migration(migrations.Migration):

    dependencies = [
        ("common", "0042_alter_project_options_project_domain_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
