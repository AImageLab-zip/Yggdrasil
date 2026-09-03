from django.apps import AppConfig


class AnnotationsConfig(AppConfig):
    """Yggdrasil's durable annotation model.

    Deliberately not part of ``common``: ``common`` is infrastructure every
    domain app already depends on, and annotations depend on it back. Keeping
    them apart is what stops the dependency from becoming a cycle when anything
    is added to ``common`` that reaches back the other way.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "annotations"
    verbose_name = "Annotations"
