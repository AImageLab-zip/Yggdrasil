"""Projects over the API: look one up, list them, create one.

Built for unattended uploaders, which hold an API token (:mod:`common.api_tokens`)
and need to know whether the project they are about to upload into exists. The
intended loop is ``GET /api/projects/<slug>/``; on 404, ``POST /api/projects/``
(superusers only), treating 409 as "someone else just created it"; then upload.

* **Existence is not leaked.** A project the caller cannot read is a 404, the
  same answer as one that does not exist -- the rule of
  ``common.project_data_api._accessible_project``.
* **Creating a project is a superuser act**, as it is in the admin
  (``DomainProjectAdmin.has_add_permission``): it is the top of the ownership
  tree.
* **Modalities and annotation methods are limited to the project's domain** plus
  the domain-blank rows, the admin's rule. A project starts with none, and the
  upload API refuses every field whose modality the project does not enable, so
  a caller that means to upload should name them here.

Shared code at a shared address: this is ``common``, not ``maxillo``, as are
the per-project endpoints under ``/api/<slug>/`` (``common/project_data_api.py``).
"""
import json

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.urls import reverse
from django.utils.text import slugify
from django.views.decorators.http import require_http_methods

from common.domains import DOMAINS, accessible_project_ids
from common.models import AnnotationMethod, Modality, Project, ProjectAccess
from common.permissions import user_has_project_access
from common.project_data_api import upload_fields

#: Slugs a project may not take, because ``/api/<slug>/…`` would collide with a
#: fixed ``/api/`` route (``/api/projects/folders/`` is this module's detail URL,
#: and the token middleware skips anything under ``/api/runner/``).
RESERVED_SLUGS = frozenset({"projects", "runner", "processing", "patients"})

NAME_MAX = Project._meta.get_field("name").max_length
SLUG_MAX = Project._meta.get_field("slug").max_length


def _error(message, status, **extra):
    return JsonResponse({"error": message, **extra}, status=status)


def _role(user, project):
    access = ProjectAccess.objects.filter(user=user, project=project).first()
    if access is not None:
        return access.role
    return "admin" if user.is_staff else None


def _serialize(project, user):
    return {
        "id": project.id,
        "slug": project.slug,
        "name": project.name,
        "domain": project.domain,
        "description": project.description,
        "is_active": project.is_active,
        "modalities": sorted(project.modalities.values_list("slug", flat=True)),
        "annotation_methods": sorted(project.annotation_methods.values_list("slug", flat=True)),
        "role": _role(user, project),
        "created_at": project.created_at.isoformat(),
    }


@require_http_methods(["GET", "POST"])
def projects_api(request):
    """``GET``: the projects the caller can read. ``POST``: create one."""
    if not request.user.is_authenticated:
        return _error("Authentication required", 401)
    if request.method == "POST":
        return _create(request)

    projects = Project.objects.all()
    ids = accessible_project_ids(request.user)
    if ids is not None:
        projects = projects.filter(id__in=ids)
    domain = request.GET.get("domain")
    if domain:
        projects = projects.filter(domain=domain)
    name = request.GET.get("name")
    if name:
        projects = projects.filter(name=name)
    return JsonResponse(
        {"projects": [_serialize(p, request.user) for p in projects.prefetch_related("modalities", "annotation_methods")]}
    )


@require_http_methods(["GET"])
def project_detail_api(request, project_slug):
    if not request.user.is_authenticated:
        return _error("Authentication required", 401)
    project = Project.objects.filter(slug=project_slug).first()
    if project is None or not user_has_project_access(request.user, project):
        return _error("Project not found", 404)
    return JsonResponse(
        {**_serialize(project, request.user), "upload_fields": upload_fields(project)}
    )


def _slugs(body, field, model, domain):
    """``(objects, None)`` for a list of slugs valid in ``domain``, else ``(None, error)``."""
    raw = body.get(field, [])
    if not isinstance(raw, list) or not all(isinstance(s, str) for s in raw):
        return None, _error(f"'{field}' must be a list of slugs", 400)
    wanted = set(raw)
    found = list(model.objects.filter(Q(domain=domain) | Q(domain=""), slug__in=wanted))
    unknown = wanted - {obj.slug for obj in found}
    if unknown:
        return None, _error(
            f"Unknown {field} for domain '{domain}'", 400, invalid=sorted(unknown)
        )
    return found, None


def _create(request):
    if not request.user.is_superuser:
        return _error("Only superusers can create projects", 403)
    try:
        body = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return _error("Body must be JSON", 400)
    if not isinstance(body, dict):
        return _error("Body must be a JSON object", 400)

    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return _error("'name' is required", 400)
    name = name.strip()
    if len(name) > NAME_MAX:
        return _error(f"'name' is longer than {NAME_MAX} characters", 400)

    domain = body.get("domain")
    if domain not in DOMAINS:
        return _error("'domain' must be one of: " + ", ".join(sorted(DOMAINS)), 400)

    slug = body.get("slug") or slugify(name)
    if not isinstance(slug, str) or slugify(slug) != slug or not slug or len(slug) > SLUG_MAX:
        return _error("'slug' must be a non-empty slug (letters, digits, '-', '_')", 400)
    if slug in RESERVED_SLUGS:
        return _error(f"'{slug}' is reserved", 400)

    description = body.get("description", "")
    if not isinstance(description, str):
        return _error("'description' must be a string", 400)

    modalities, error = _slugs(body, "modalities", Modality, domain)
    if error:
        return error
    methods, error = _slugs(body, "annotation_methods", AnnotationMethod, domain)
    if error:
        return error

    existing = Project.objects.filter(Q(slug=slug) | Q(domain=domain, name=name)).first()
    if existing is not None:
        return _error("Project already exists", 409, slug=existing.slug)
    try:
        with transaction.atomic():
            project = Project.objects.create(
                name=name,
                slug=slug,
                domain=domain,
                description=description,
                created_by=request.user,
            )
            project.modalities.set(modalities)
            project.annotation_methods.set(methods)
            ProjectAccess.objects.create(user=request.user, project=project, role="admin")
    except IntegrityError:
        # Lost a race with another creator between the check and the insert.
        existing = Project.objects.filter(Q(slug=slug) | Q(domain=domain, name=name)).first()
        return _error("Project already exists", 409, slug=existing.slug if existing else slug)

    response = JsonResponse(_serialize(project, request.user), status=201)
    response["Location"] = reverse("project_detail_api", args=[project.slug])
    return response
