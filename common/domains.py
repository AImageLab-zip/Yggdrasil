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
