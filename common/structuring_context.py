"""Whether the patient-detail page may offer report structuring.

One function, called by all four domains' detail views, because the alternative is four
copies of the same three conditions drifting apart. Commit ``1de0b57`` is what that
drift looks like in practice: the UI flag said ``or True`` while the POST honoured the
real setting, so the control was visible and refused.

The same ``structuring_enabled_for`` that this calls is what
``common/domain_views/caption_reports.py`` calls before honouring a request. The button
and the refusal cannot disagree, because they ask the same question of the same code.
"""

from common.external_config import llm_service, structuring_enabled_for
from common.llm_tasks import CAPTION_TO_TEMPLATE
from common.report_templates import modality_slugs_with_templates


def structuring_context(patient):
    """``{'structuring_available': bool, 'structuring_modalities': [...]}``.

    Three things must all hold: the project has the feature ticked, a language model is
    configured *and* reachable in principle (a key present in the environment), and the
    domain has at least one report template to file into. Any of them missing and the
    control is simply absent -- there is nothing useful the clinician could do with it.

    ``structuring_modalities`` is the list of modality slugs that have a template, or
    ``["*"]`` when one covers them all. Urology changes modality without a page load, so
    the browser needs the list rather than a single yes/no.
    """
    project = getattr(patient, "project", None)
    domain = patient._meta.app_label

    enabled = structuring_enabled_for(patient)
    # Resolved, not merely present: a disabled row or an empty key variable means the
    # POST would answer 503, and a button that can only fail is worse than no button.
    configured = llm_service(CAPTION_TO_TEMPLATE.service_slug) is not None
    modalities = modality_slugs_with_templates(domain, project) if enabled else []

    return {
        "structuring_available": bool(enabled and configured and modalities),
        "structuring_modalities": modalities,
    }
