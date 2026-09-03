"""Project-driven filter configuration for the shared patient-list filter bar.

Projects only collect a subset of what the platform supports: ``tf4_testset``
collects CBCT + panoramic and voice captions, nothing else. A filter for data a
project never gathers is noise that always returns nothing, so the filter bar is
built from the project's own ``Modality`` and ``AnnotationMethod`` sets rather
than from a per-domain hardcoded list.

Shared by ``maxillo.views.patient_list`` (also serving laparoscopy) and
``brain.views.patient_list``, which render the same
``common/partials/patient_list_content.html``.
"""

# Annotation-presence filters, keyed by the AnnotationMethod slug that has to be
# enabled for the project before the filter is offered. ``modality``, when
# present, additionally requires that modality: IOS landmarks are impossible
# without IOS scans.
#
# ``key`` is the DOM id prefix static/js/patient_list.js binds to. 'reports' is
# the one legacy key (its hidden input is `reportsFilterValue`, special-cased in
# that script); everything else follows the `<key>_value` convention.
PRESENCE_FILTERS = [
    {
        "key": "reports",
        "param": "has_reports",
        "method": "voice_caption",
        "label": "Reports",
        "icon": "fas fa-comment",
    },
    {
        "key": "presence_bite_classification",
        "param": "has_bite_classification",
        "method": "bite_classification",
        "label": "Bite classification",
        "icon": "fas fa-teeth",
    },
    {
        "key": "presence_landmarks",
        "param": "has_landmarks",
        "method": "ios_landmarks",
        "modality": "ios",
        "label": "Landmarks",
        "icon": "fas fa-location-dot",
    },
    {
        "key": "presence_segmentation",
        "param": "has_segmentation",
        "method": "intraoral_segmentation",
        "modality": "intraoral-photo",
        "label": "Tooth segmentation",
        "icon": "fas fa-draw-polygon",
    },
]


def presence_filter_specs(request, project, allowed_modality_slugs):
    """Presence filters this project actually collects, in declaration order.

    Absent a project the list is empty and the filter bar omits the group.
    Each spec carries the current value from the query string so the template can
    render the button in its active state.
    """
    if project is None:
        return []
    enabled_methods = set(
        project.annotation_methods.filter(is_active=True).values_list("slug", flat=True)
    )
    allowed_modality_slugs = set(allowed_modality_slugs or ())
    specs = []
    for spec in PRESENCE_FILTERS:
        if spec["method"] not in enabled_methods:
            continue
        required_modality = spec.get("modality")
        if required_modality and required_modality not in allowed_modality_slugs:
            continue
        specs.append({**spec, "value": request.GET.get(spec["param"], "").strip()})
    return specs
