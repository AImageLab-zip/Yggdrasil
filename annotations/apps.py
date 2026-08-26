from django.apps import AppConfig


class AnnotationsConfig(AppConfig):
    """Yggdrasil's durable annotation model.

    Deliberately not part of ``common``: ``common`` is infrastructure every
    domain app already depends on, and annotations depend on it back. Keeping
    them apart is what stops the dependency from becoming a cycle when Phase 8
    adds the DICOM catalog to ``common``.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "annotations"
    verbose_name = "Annotations"
