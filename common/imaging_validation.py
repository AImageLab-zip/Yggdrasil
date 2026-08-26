"""The Phase 3 validation harness page (docs/cornerstone-roadmap.md, Phase 3).

Phase 3 replaces the CBCT and brain volume grid with Cornerstone3D and deletes NiiVue
outright. Decision #3 rules out feature flags -- each surface is replaced and its old
code deleted in the same commit -- so there is no runtime fallback and no way to
compare the two stacks after the fact. The roadmap therefore makes one pre-merge check
the entire safety net:

    Gate: the validation harness must be green across the maxillo *and* brain
    corpora before this merges.

This module is the server half of that harness. It selects the volumes to run and
renders a page; all the comparison happens in the browser, in
``frontend/imaging/validation/``, because that is where both viewers actually live.
Shape follows ``maxillo/views/panoramic_warmup.py``: an admin page that drives real
studies through the real code path, rather than a second implementation to keep in
sync.

**Temporary.** This page, the ``volume-validation`` bundle entry and the vendored
NiiVue all go when the gate is cleared and the viewer replacement merges.

Why staff-only, and not the project-admin gate ``panoramic_warmup`` uses: finding F10.
``common/demo.py`` ``demo_index`` logs an anonymous visitor in as a real user, so every
new ``@login_required`` endpoint is instantly anonymous-public for demo folders. This
page enumerates raw volume URLs across all three domains, which is precisely the shape
that must not be. The demo guest is an ordinary non-staff user, so ``is_staff`` is a
gate the demo path cannot reach at all.
"""

import logging

from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from common.models import FileRegistry

logger = logging.getLogger(__name__)

#: A run is bounded so one page load cannot queue an unbounded amount of browser work.
#: Each study is fetched three times and held in GPU memory twice, so the ceiling is
#: much lower than ``panoramic_warmup``'s.
VALIDATION_BATCH_LIMIT = 25

#: The file types the volume grid renders, per domain. Anything not listed here is not
#: a volume Phase 3 has to display, so running it would pad the report without adding
#: coverage.
VOLUME_FILE_TYPES = {
    "maxillo": ("cbct_raw", "cbct_processed"),
    "brain": (
        "braintumor_mri_t1_raw",
        "braintumor_mri_t1ce_raw",
        "braintumor_mri_t2_raw",
        "braintumor_mri_flair_raw",
        "braintumor_mri_seg",
    ),
}


def _is_staff(user):
    return bool(user.is_authenticated and user.is_staff)


staff_required = user_passes_test(_is_staff)


def _bundle_members(file_obj):
    """The addressable members of a multi-file row, as ``(key, filename)`` pairs.

    A ``cbct_processed`` row with ``file_hash == 'multi-file'`` holds several volumes,
    and the maxillo CBCT *display* volume is one of them
    (``maxillo/views/patient_detail.py:_resolved_cbct_viewer_source``). Each member is
    a separate volume for the harness, addressed through the query-free bundle route
    added for finding F14.
    """
    metadata = file_obj.metadata if isinstance(file_obj.metadata, dict) else {}
    files = metadata.get("files")
    if not isinstance(files, dict):
        return []

    members = []
    for key, entry in files.items():
        if key == "primary" or not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path.endswith((".nii", ".nii.gz")):
            continue
        members.append((key, path.rsplit("/", 1)[-1]))
    return sorted(members)


def _study_entries(file_obj):
    """Every volume one FileRegistry row exposes, as harness study descriptors."""
    namespace = file_obj.domain if file_obj.domain in ("maxillo", "brain") else "api"
    members = _bundle_members(file_obj)

    if members:
        return [
            {
                "study": f"{file_obj.domain}/{file_obj.id}/{key}",
                "fileId": file_obj.id,
                "filename": filename,
                "namespace": namespace,
                "bundleKey": key,
                "fileType": file_obj.file_type,
                "domain": file_obj.domain,
            }
            for key, filename in members
        ]

    path = file_obj.file_path or ""
    if not path.endswith((".nii", ".nii.gz")):
        # Not a volume the grid renders -- a zip, a PNG, a bundle with no NIfTI in it.
        return []

    return [
        {
            "study": f"{file_obj.domain}/{file_obj.id}",
            "fileId": file_obj.id,
            "filename": path.rsplit("/", 1)[-1],
            "namespace": namespace,
            # 'primary' is the sentinel for an ordinary single-file row; the client
            # maps it onto the plain serve route.
            "bundleKey": "primary",
            "fileType": file_obj.file_type,
            "domain": file_obj.domain,
        }
    ]


def candidate_studies(domain, limit=VALIDATION_BATCH_LIMIT):
    """Volumes from one domain, newest first, bounded.

    Deliberately **not** filtered to "interesting" studies. The gate is about the
    corpus as it is, and a harness that quietly skipped the volumes it found awkward
    would be reporting on a corpus nobody has.
    """
    file_types = VOLUME_FILE_TYPES.get(domain)
    if not file_types:
        return []

    rows = (
        FileRegistry.objects.filter(domain=domain, file_type__in=file_types)
        .order_by("-created_at", "-id")[: limit * 2]
    )

    studies = []
    for row in rows:
        studies.extend(_study_entries(row))
        if len(studies) >= limit:
            break
    return studies[:limit]


@login_required
@staff_required
@require_GET
def imaging_validation(request):
    """Render the harness page. The comparison itself runs in the browser."""
    limit = VALIDATION_BATCH_LIMIT
    try:
        limit = max(1, min(VALIDATION_BATCH_LIMIT, int(request.GET.get("limit", limit))))
    except (TypeError, ValueError):
        pass

    domains = [domain for domain in ("maxillo", "brain") if domain in VOLUME_FILE_TYPES]
    corpora = {domain: candidate_studies(domain, limit) for domain in domains}

    return render(
        request,
        "common/imaging_validation.html",
        {
            "corpora": corpora,
            "validation_data": {
                "studies": [study for studies in corpora.values() for study in studies],
                "byDomain": corpora,
                "limit": limit,
            },
            # The gate names both corpora explicitly, so an empty one has to be visible
            # on the page rather than inferred from a study count.
            "empty_domains": [domain for domain, studies in corpora.items() if not studies],
        },
    )


@login_required
@staff_required
@require_GET
def imaging_validation_studies(request):
    """The same corpus as JSON, for re-running without reloading the page."""
    domain = request.GET.get("domain") or ""
    if domain and domain not in VOLUME_FILE_TYPES:
        return JsonResponse({"error": f"Unknown domain '{domain}'."}, status=400)

    domains = [domain] if domain else list(VOLUME_FILE_TYPES)
    corpora = {name: candidate_studies(name) for name in domains}
    return JsonResponse(
        {
            "byDomain": corpora,
            "studies": [study for studies in corpora.values() for study in studies],
        }
    )
