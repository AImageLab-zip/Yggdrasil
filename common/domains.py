"""Single source of truth for the domain registry (Phase 5.2).

Historically ``DOMAIN_CHOICES`` and the hardcoded ``{"maxillo", "brain",
"laparoscopy"}`` set were copy-pasted across models, permissions and job
routing. This module centralizes them so adding a domain is a one-line change
here (plus the per-domain FK columns on Job/ProcessingJob/FileRegistry).
"""

# Order matters: first entry is the historical default.
DOMAIN_CHOICES = [
    ("maxillo", "Maxillo"),
    ("brain", "Brain"),
    ("laparoscopy", "Laparoscopy"),
]

DEFAULT_DOMAIN = DOMAIN_CHOICES[0][0]

# frozenset of valid domain slugs, e.g. {"maxillo", "brain", "laparoscopy"}.
DOMAINS = frozenset(slug for slug, _ in DOMAIN_CHOICES)

# Per-domain FK field names on Job / ProcessingJob / FileRegistry.
# domain -> (patient_fk_name, voice_caption_fk_name)
DOMAIN_FK_FIELDS = {
    "maxillo": ("patient", "voice_caption"),
    "brain": ("brain_patient", "brain_voice_caption"),
    "laparoscopy": ("laparoscopy_patient", "laparoscopy_voice_caption"),
}


def normalize_domain(value):
    """Return ``value`` if it is a known domain slug, else ``DEFAULT_DOMAIN``."""
    return value if value in DOMAINS else DEFAULT_DOMAIN


def fk_fields_for(domain):
    """Return ``(patient_fk, voice_caption_fk)`` for ``domain`` (default-safe)."""
    return DOMAIN_FK_FIELDS.get(normalize_domain(domain), DOMAIN_FK_FIELDS[DEFAULT_DOMAIN])


def order_projects_for_landing(queryset):
    """Order a Project queryset in the canonical landing order.

    Known domains follow DOMAIN_CHOICES order (maxillo, brain, laparoscopy);
    any future project sorts after them, alphabetically.
    """
    from django.db.models import Case, IntegerField, Value, When

    whens = [When(slug=slug, then=Value(i)) for i, (slug, _) in enumerate(DOMAIN_CHOICES)]
    return queryset.annotate(
        _landing_rank=Case(*whens, default=Value(len(DOMAIN_CHOICES)), output_field=IntegerField())
    ).order_by("_landing_rank", "name")


# Fallback copy for domains whose Project row has no description set.
_DOMAIN_BLURBS = {
    "maxillo": "Dental & maxillofacial imaging — bite classification, IOS, CBCT and panoramic extraction.",
    "brain": "Brain tumor MRI — multi-sequence review with AI-assisted captioning.",
    "laparoscopy": "Surgical video annotation — frame-accurate segmentation and tagging.",
}

# Default glyph per domain when Project.icon is blank.
_DOMAIN_ICONS = {
    "maxillo": "fas fa-tooth",
    "brain": "fas fa-brain",
    "laparoscopy": "fas fa-video",
}


def patient_count_for(slug):
    """Return the patient count for a domain, or None if it can't be determined.

    Patients live in per-app tables with no FK back to Project, so this resolves
    the domain's Patient model by app label. Best-effort: the landing page must
    never 500 because a count failed.
    """
    from django.apps import apps

    try:
        return apps.get_model(slug, "Patient").objects.count()
    except Exception:  # noqa: BLE001 - unknown/legacy domain, or table absent
        return None


def landing_cards(projects):
    """Build the landing page's domain cards from real Project rows.

    Returns dicts of {project, slug, name, icon, blurb, stat}. `stat` is a true
    patient count (never a placeholder); it is omitted when unavailable so the
    card renders without a fabricated figure.
    """
    cards = []
    for project in projects:
        slug = project.slug or ""
        count = patient_count_for(slug)
        if count is None:
            stat = ""
        elif count == 1:
            stat = "1 patient"
        else:
            stat = "{:,} patients".format(count)
        cards.append(
            {
                "project": project,
                "slug": slug,
                "name": project.name,
                "icon": project.icon or _DOMAIN_ICONS.get(slug, "fas fa-folder-open"),
                "blurb": project.description or _DOMAIN_BLURBS.get(slug, ""),
                "stat": stat,
            }
        )
    return cards
