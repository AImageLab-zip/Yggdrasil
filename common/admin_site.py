"""The admin index, grouped by what a thing *is* rather than which app declares it.

Django's default index is a list of installed apps. That is an accurate map of
the source tree and a poor map of the product: ``Job``, ``FileRegistry`` and
``Modality`` are declared in ``common`` and have nothing to do with each other,
while a maxillo patient, a brain patient and a laparoscopy patient are the same
kind of thing filed under three separate headings. An admin looking for "the
projects" had to know that projects live in four places, and an admin looking at
"Common" was shown the imaging catalog, the scheduler and the site-maintenance
switch side by side.

:class:`YggdrasilAdminSite` keeps every registration exactly where it is and
only rewrites the *index*: models are placed into named sections by purpose, in
a stated order, and anything not named -- ``auth`` today, a new app tomorrow --
still appears, under its own app heading, after the sections. Nothing here can
hide a model: a registration missing from :data:`SECTIONS` falls through to the
leftovers rather than disappearing.

The per-app index page (``/admin/common/``) is untouched; ``get_app_list`` only
regroups when it is asked for the whole list.
"""

from django.contrib.admin import AdminSite
from django.urls import reverse

SITE_HEADER = "Yggdrasil administration"
SITE_TITLE = "Yggdrasil admin"
INDEX_TITLE = "Platform administration"

#: The index, in order. Each section is ``(slug, heading, members)`` and each
#: member is ``(app_label, object_name)``; ``(app_label, "*")`` takes every
#: still-unplaced model of that app, sorted by name, so a model added to that
#: app appears without anyone remembering to edit this file.
#:
#: A model may be named once. Names that match no registration are skipped in
#: silence -- that is what lets this list mention ``laparoscopy.RegionType``
#: while a deployment that has not registered it still renders.
SECTIONS = (
    (
        "projects",
        "Projects & access",
        (
            ("common", "Project"),
            ("maxillo", "MaxilloProject"),
            ("brain", "BrainProject"),
            ("laparoscopy", "LaparoscopyProject"),
            ("common", "ProjectAccess"),
            ("common", "Invitation"),
            ("maxillo", "Folder"),
            ("brain", "Folder"),
            ("laparoscopy", "Folder"),
        ),
    ),
    (
        "clinical",
        "Clinical data",
        (
            ("maxillo", "Patient"),
            ("brain", "Patient"),
            ("laparoscopy", "Patient"),
            ("maxillo", "Classification"),
            ("laparoscopy", "Classification"),
            ("maxillo", "IntraoralToothSegmentation"),
            ("laparoscopy", "QuadrantType"),
            ("laparoscopy", "RegionType"),
            ("laparoscopy", "QuadrantClassificationMarker"),
            ("maxillo", "VoiceCaption"),
            ("brain", "VoiceCaption"),
            ("laparoscopy", "VoiceCaption"),
            ("maxillo", "Export"),
            ("brain", "Export"),
            ("laparoscopy", "Export"),
            ("maxillo", "Dataset"),
            ("brain", "Dataset"),
            ("laparoscopy", "Dataset"),
            ("maxillo", "Tag"),
            ("brain", "Tag"),
            ("laparoscopy", "Tag"),
        ),
    ),
    (
        "annotations",
        "Annotations",
        (
            ("annotations", "AnnotationSet"),
            ("annotations", "AnnotationTarget"),
            ("annotations", "AnnotationSelector"),
            ("annotations", "AnnotationRevision"),
            ("annotations", "AnnotationPayload"),
            ("annotations", "LabelSchema"),
            ("annotations", "LabelDefinition"),
            ("annotations", "SourceResource"),
            # Anything else the app registers -- the five item tables today.
            ("annotations", "*"),
        ),
    ),
    (
        "imaging",
        "Imaging catalog",
        (("common", "FileRegistry"),),
    ),
    (
        "processing",
        "Processing",
        (
            ("common", "Modality"),
            ("common", "ProcessingStep"),
            ("common", "AnnotationMethod"),
            ("common", "Job"),
            ("common", "ProcessingJob"),
        ),
    ),
    (
        "operations",
        "Operations",
        (
            ("common", "SystemCheck"),
            ("common", "SiteMaintenance"),
            ("common", "UserSession"),
            ("common", "ActivityEvent"),
            ("common", "Notification"),
            ("common", "UserPreference"),
            ("common", "RecentlyViewed"),
        ),
    ),
)


class YggdrasilAdminSite(AdminSite):
    """The default site, with a purpose-ordered index."""

    site_header = SITE_HEADER
    site_title = SITE_TITLE
    index_title = INDEX_TITLE

    def get_app_list(self, request, app_label=None):
        # The single-app index page asks for one app and wants Django's answer.
        if app_label is not None:
            return super().get_app_list(request, app_label)

        app_dict = self._build_app_dict(request)
        entries = {}
        for label, app in app_dict.items():
            for entry in app["models"]:
                model = entry.get("model")
                name = (
                    model._meta.object_name
                    if model is not None
                    else entry["object_name"]
                )
                entries[(label, name)] = entry

        placed = set()
        sections = []
        for slug, heading, members in SECTIONS:
            models = []
            for app_name, object_name in members:
                if object_name == "*":
                    rest = [
                        key
                        for key in entries
                        if key[0] == app_name and key not in placed
                    ]
                    rest.sort(key=lambda key: entries[key]["name"].lower())
                    for key in rest:
                        placed.add(key)
                        models.append(entries[key])
                    continue
                key = (app_name, object_name)
                if key in entries and key not in placed:
                    placed.add(key)
                    models.append(entries[key])
            if models:
                sections.append(
                    {
                        "name": heading,
                        "app_label": slug,
                        # A fragment, not "": the index template marks a section
                        # "current" when `app_url in request.path`, and the empty
                        # string is in every path.
                        "app_url": f"{self._index_url(request)}#{slug}",
                        "has_module_perms": True,
                        "models": models,
                    }
                )

        # Whatever the sections did not name keeps its own app heading, after
        # them. `auth` lands here, and so does any app added later.
        leftovers = []
        for label, app in sorted(app_dict.items(), key=lambda kv: kv[1]["name"].lower()):
            models = [
                entry
                for entry in app["models"]
                if (label, self._entry_name(entry)) not in placed
            ]
            if not models:
                continue
            rest = dict(app)
            rest["models"] = models
            leftovers.append(rest)

        return sections + leftovers

    @staticmethod
    def _entry_name(entry):
        model = entry.get("model")
        return model._meta.object_name if model is not None else entry["object_name"]

    def _index_url(self, request):
        try:
            return reverse(f"{self.name}:index", current_app=self.name)
        except Exception:  # pragma: no cover - the admin is always routed
            return "/admin/"


def install(site):
    """Make ``site`` behave as a :class:`YggdrasilAdminSite`.

    ``django.contrib.admin.site`` is instantiated from
    ``AppConfig.default_site`` before any project code runs, so the supported
    way to replace it is a one-line ``INSTALLED_APPS`` swap: an
    ``AdminConfig`` subclass whose ``default_site`` names
    ``common.admin_site.YggdrasilAdminSite``, listed in place of
    ``django.contrib.admin``. ``settings.py`` is shared, so this gets the same
    result without touching it: rebinding the
    class of an ``AdminSite`` instance is safe because the subclass adds no
    state, only ``get_app_list`` and three labels. If the settings swap is made
    later, ``site`` is already the right class and this is a no-op.
    """
    if not isinstance(site, YggdrasilAdminSite):
        site.__class__ = YggdrasilAdminSite
    site.site_header = SITE_HEADER
    site.site_title = SITE_TITLE
    site.index_title = INDEX_TITLE
    return site
