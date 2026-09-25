"""Central accessors for admin-driven external-service config.

The counterpart of ``common/modality_config.py``, for the third nature of processing: a
service that has to answer while the clinician waits. Callers ask for a service by slug
and get a :class:`ResolvedService` -- a frozen value object carrying the endpoint, the
key read from the environment, the timeouts and the validated parameters -- or ``None``.
Nothing outside this module reads ``common.ExternalService``.

**The resolution rule, stated once.**

* An **enabled row wins entirely.** Every field comes from it; the environment is not
  consulted, not even for a blank.
* **No row at all** falls back to whatever the caller supplies as ``env_fallback``, which
  is how a deployment that has never opened the admin keeps working.
* A **disabled row resolves to ``None``**, and the environment does not rescue it.
  Otherwise the tick box is a lie: an admin who turns a service off and watches it keep
  answering has been told something false by the interface.

Resolution returns ``None`` rather than a half-built object when the endpoint is blank,
the kind needs a key and the named environment variable is empty, or the row is gone.
Every caller therefore has exactly one thing to check, and the feature fails closed --
the same shape as the relay's 4503 and laparoscopy's unset ``WORKER_BASE_URL``.
"""
import logging
import os

from django.db.utils import DatabaseError

from common import external_services

logger = logging.getLogger(__name__)

#: Slugs the platform itself looks for. A deployment may add others.
WHISPER_SERVICE_SLUG = "whisper_live"
LLM_SERVICE_SLUG = "openrouter_llm"


class ResolvedService:
    """An external service, ready to call. Immutable by construction, not by ceremony."""

    __slots__ = (
        "slug", "kind", "base_url", "api_key", "model_name", "parameters",
        "languages", "timeout", "connect_timeout", "ca_cert", "source", "row_id",
    )

    def __init__(self, *, slug, kind, base_url, api_key, model_name, parameters,
                 languages, timeout, connect_timeout, ca_cert, source, row_id=None):
        object.__setattr__(self, "slug", slug)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "model_name", model_name)
        object.__setattr__(self, "parameters", dict(parameters or {}))
        object.__setattr__(self, "languages", tuple(languages or ()))
        object.__setattr__(self, "timeout", timeout)
        object.__setattr__(self, "connect_timeout", connect_timeout)
        object.__setattr__(self, "ca_cert", ca_cert)
        object.__setattr__(self, "source", source)          # 'db' | 'env'
        object.__setattr__(self, "row_id", row_id)

    def __setattr__(self, name, value):  # pragma: no cover - guard, not behaviour
        raise AttributeError(
            "ResolvedService is a snapshot of configuration; change the admin row instead"
        )

    def parameter(self, name, default=None):
        value = self.parameters.get(name, default)
        return default if value is None else value

    def __repr__(self):  # pragma: no cover - debugging only; never prints the key
        return (
            f"<ResolvedService {self.slug} kind={self.kind} "
            f"url={self.base_url!r} source={self.source} key={'set' if self.api_key else 'unset'}>"
        )


def get_service(slug):
    """The ``ExternalService`` row for ``slug``, or ``None``.

    Swallows ``DatabaseError`` -- a read before the table is migrated, most often -- and
    reports it as absent, which lets the environment fallback carry a deployment through
    the migration that introduces this table.
    """
    slug = str(slug or "").strip()
    if not slug:
        return None
    from common.models import ExternalService

    try:
        return ExternalService.objects.filter(slug=slug).first()
    except DatabaseError:
        logger.warning("ExternalService lookup failed for '%s'; falling back to settings", slug)
        return None


def resolve(slug, *, env_fallback=None):
    """A :class:`ResolvedService` for ``slug``, or ``None`` when it is unusable.

    ``env_fallback`` is a zero-argument callable returning a ResolvedService built from
    ``settings``. It is used only when there is no row at all -- see the module docstring
    for why a *disabled* row is not the same as a missing one.
    """
    row = get_service(slug)
    if row is None:
        return env_fallback() if env_fallback is not None else None
    if not row.is_enabled:
        logger.info("External service '%s' is disabled; refusing to resolve it", slug)
        return None

    kind = row.service_kind
    if kind is None:
        logger.error("External service '%s' declares unknown kind '%s'", slug, row.kind)
        return None
    if not row.base_url:
        logger.error("External service '%s' has no base URL", slug)
        return None

    api_key = os.environ.get(row.api_key_env, "") if row.api_key_env else ""
    if kind.requires_api_key and not api_key:
        logger.error(
            "External service '%s' needs a key in $%s, which is empty",
            slug, row.api_key_env or "(unset)",
        )
        return None
    if kind.requires_model and not row.model_name:
        logger.error("External service '%s' needs a model name", slug)
        return None

    parameters = dict(kind.defaults())
    parameters.update(row.parameters or {})

    return ResolvedService(
        slug=row.slug,
        kind=row.kind,
        base_url=row.base_url,
        api_key=api_key,
        model_name=row.model_name,
        parameters=parameters,
        languages=row.languages or (),
        timeout=row.timeout_seconds,
        connect_timeout=row.connect_timeout_seconds,
        ca_cert=row.ca_cert_path,
        source="db",
        row_id=row.pk,
    )


def _whisper_from_settings():
    """The pre-registry configuration: ``settings.WHISPER_*``, verbatim.

    Kept as the fallback so a deployment that never adds a row behaves exactly as it did
    before this module existed, including the 4503 on a missing token.
    """
    from django.conf import settings

    url = getattr(settings, "WHISPER_WS_URL", "")
    token = getattr(settings, "WHISPER_API_TOKEN", "")
    if not url or not token:
        return None
    return ResolvedService(
        slug=WHISPER_SERVICE_SLUG,
        kind=external_services.STT_WEBSOCKET.key,
        base_url=url,
        api_key=token,
        model_name="",
        parameters=external_services.STT_WEBSOCKET.defaults(),
        languages=(),
        timeout=getattr(settings, "WHISPER_CONNECT_TIMEOUT", 10),
        connect_timeout=getattr(settings, "WHISPER_CONNECT_TIMEOUT", 10),
        ca_cert=getattr(settings, "WHISPER_CA_CERT", ""),
        source="env",
    )


def whisper_service():
    """The speech-to-text service for live dictation, or ``None`` if unconfigured."""
    return resolve(WHISPER_SERVICE_SLUG, env_fallback=_whisper_from_settings)


def _llm_from_settings():
    from django.conf import settings

    base_url = getattr(settings, "OPENROUTER_BASE_URL", "")
    key_env = getattr(settings, "OPENROUTER_API_KEY_ENV", "OPENROUTER_API_KEY")
    model = getattr(settings, "OPENROUTER_MODEL", "")
    api_key = os.environ.get(key_env, "")
    if not (base_url and model and api_key):
        return None
    return ResolvedService(
        slug=LLM_SERVICE_SLUG,
        kind=external_services.LLM_CHAT_OPENAI.key,
        base_url=base_url,
        api_key=api_key,
        model_name=model,
        parameters=external_services.LLM_CHAT_OPENAI.defaults(),
        languages=(),
        timeout=getattr(settings, "LLM_TIMEOUT", 60),
        connect_timeout=getattr(settings, "LLM_CONNECT_TIMEOUT", 10),
        ca_cert="",
        source="env",
    )


def llm_service(slug=LLM_SERVICE_SLUG):
    """An OpenAI-compatible chat service, or ``None`` if unconfigured."""
    return resolve(slug, env_fallback=_llm_from_settings)


#: The per-project switch, ticked on the project's annotation methods in the admin.
STRUCTURING_METHOD_SLUG = "report_structuring"


def structuring_enabled_for(patient):
    """Whether this patient's project has report structuring turned on.

    **Not** ``project_allows_annotation``, deliberately. That helper returns ``True`` when
    ``patient.project`` is ``None`` -- permissive by design, so legacy rows with no project
    keep working. Permissive-by-default is the wrong direction for a feature that sends
    clinical text to a third-party API: a patient nobody assigned to a project would be
    opted in by accident, which is exactly the case nobody would notice.

    One function, called by both the view that honours the POST and the context that
    decides whether to render the button. Commit ``1de0b57`` is what happens when those
    two answers are written twice and drift: the UI said yes while the server said no.
    """
    return structuring_enabled_for_project(getattr(patient, "project", None))


def structuring_enabled_for_project(project):
    """The same answer for a project, for pages that list many patients of it.

    ``structuring_enabled_for`` delegates here, so the patient list and the patient page
    cannot disagree about a project: there is one rule, asked two ways.
    """
    if project is None:
        return False
    try:
        return project.allows_annotation(STRUCTURING_METHOD_SLUG)
    except DatabaseError:
        logger.warning("Annotation-method lookup failed; refusing structuring")
        return False
