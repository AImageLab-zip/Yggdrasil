"""A project's folders, patients and uploads over the API, for every domain.

``/api/<project_slug>/folders/``, ``/patients/``, ``/patients/<id>/files/`` and
``/upload/``. The project's own ``domain`` picks the tables (``apps.get_model``,
app label == domain slug) and, for uploads, the domain's
:class:`common.domain_uploads.UploadHandler`. The first version of these views
lived in ``maxillo`` and always used maxillo's models, so a brain project's
upload landed in maxillo's tables.

Same rules as :mod:`common.project_api`: 401 without a user, 404 for a project
the caller cannot read (indistinguishable from a missing one), JSON everywhere.
Writes take the folder's project as the authority: creating a folder needs the
project's admin role, uploading needs a writing role on the target folder.
"""
import json
import logging
import os

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from common.base_forms import _tag_names, _tags
from common.domain_uploads import UploadRejected, UploadResult, get_handler
from common.models import Project
from common.permissions import (
    filter_folders_for_user,
    filter_patients_for_user,
    user_can_read_patient,
    user_can_write_annotations,
    user_has_project_access,
    user_is_project_admin,
)

logger = logging.getLogger(__name__)

#: Most patients ``POST /patients/`` returns files for in one request.
MAX_BULK_PATIENTS = 100


def _error(message, status, **extra):
    return JsonResponse({"error": message, **extra}, status=status)


def _accessible_project(request, project_slug):
    """``(project, None)`` for a project the user may read, else ``(None, response)``."""
    if not request.user.is_authenticated:
        return None, _error("Authentication required", 401)
    project = Project.objects.filter(slug=project_slug).first()
    if project is None or not user_has_project_access(request.user, project):
        return None, _error("Project not found", 404)
    return project, None


def _model(project, name):
    return apps.get_model(project.domain, name)


def _project_ref(project):
    return {"id": project.id, "name": project.name, "slug": project.slug, "domain": project.domain}


def _folder_ref(folder):
    if folder is None:
        return None
    return {"id": folder.id, "name": folder.name, "full_path": folder.get_full_path()}


def _modality_ref(modality):
    return {
        "id": modality.id,
        "name": modality.name,
        "slug": modality.slug,
        "label": modality.label,
    }


def _patient_ref(patient):
    return {
        "patient_id": patient.patient_id,
        "name": patient.name,
        "uploaded_at": patient.uploaded_at.isoformat(),
        "folder": _folder_ref(patient.folder),
        "modalities": [_modality_ref(m) for m in patient.modalities.all()],
    }


def _file_ref(file_obj):
    return {
        "id": file_obj.id,
        "filename": os.path.basename(file_obj.file_path),
        "file_type": file_obj.file_type,
        "file_type_display": file_obj.get_file_type_display(),
        "file_size": file_obj.file_size,
        "file_hash": file_obj.file_hash,
        "subtype": file_obj.subtype,
        "created_at": file_obj.created_at.isoformat(),
        "metadata": file_obj.metadata,
        "modality": _modality_ref(file_obj.modality) if file_obj.modality else None,
    }


def upload_fields(project):
    """What ``POST /upload/`` accepts for this project, for the project detail."""
    handler = get_handler(project.domain)
    if handler is None:
        return []
    return [spec.describe(modality) for spec, modality in handler.fields_for(project)]


# --------------------------------------------------------------------------
# Folders
# --------------------------------------------------------------------------


@require_http_methods(["GET", "POST"])
def project_folders(request, project_slug):
    """``GET``: the project's folders. ``POST {"name"}``: get-or-create one."""
    project, denied = _accessible_project(request, project_slug)
    if denied:
        return denied
    Folder = _model(project, "Folder")
    if request.method == "POST":
        return _create_folder(request, project, Folder)

    folders = filter_folders_for_user(
        request.user,
        Folder.objects.filter(parent__isnull=True, project=project).order_by("name"),
        project.domain,
    )
    folders_data = [
        {**_folder_ref(folder), "created_at": folder.created_at.isoformat()}
        for folder in folders
    ]
    return JsonResponse({
        "success": True,
        "project": _project_ref(project),
        "folders": folders_data,
        "total_folders": len(folders_data),
    })


def _create_folder(request, project, Folder):
    if not user_is_project_admin(request.user, project):
        return _error("Only project admins can create folders", 403)
    try:
        body = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return _error("Body must be JSON", 400)
    name = body.get("name") if isinstance(body, dict) else None
    if not isinstance(name, str) or not name.strip():
        return _error("'name' is required", 400)
    name = name.strip()
    max_length = Folder._meta.get_field("name").max_length
    if max_length and len(name) > max_length:
        return _error(f"'name' is longer than {max_length} characters", 400)

    # Folders are single-level (see common/domain_views/folders_tags.py).
    folder, created = Folder.objects.get_or_create(
        name=name, parent=None, project=project,
        defaults={"created_by": request.user},
    )
    return JsonResponse(
        {"success": True, "created": created, "folder": _folder_ref(folder)},
        status=201 if created else 200,
    )


# --------------------------------------------------------------------------
# Patients and their files
# --------------------------------------------------------------------------


def _readable_patients(request, project):
    Patient = _model(project, "Patient")
    return filter_patients_for_user(
        request.user, Patient.objects.filter(project=project), project.domain
    )


@require_http_methods(["GET", "POST"])
def project_patients(request, project_slug):
    """``GET``: the project's patients, optionally ``?folder=<id>`` and
    ``?name=<exact name>``. ``POST {"patient_ids": [...]}``: their files."""
    project, denied = _accessible_project(request, project_slug)
    if denied:
        return denied
    if request.method == "POST":
        return _patients_files(request, project)

    patients = _readable_patients(request, project)
    folder_id = request.GET.get("folder")
    if folder_id:
        if not folder_id.isdigit():
            return _error("'folder' must be a folder id", 400)
        patients = patients.filter(folder_id=int(folder_id))
    name = request.GET.get("name")
    if name:
        patients = patients.filter(name=name)
    patients = patients.select_related("folder").prefetch_related("modalities").order_by("patient_id")

    patients_data = [_patient_ref(p) for p in patients]
    return JsonResponse({
        "success": True,
        "project": _project_ref(project),
        "patients": patients_data,
        "total_patients": len(patients_data),
    })


@require_http_methods(["GET"])
def patient_files(request, project_slug, patient_id):
    project, denied = _accessible_project(request, project_slug)
    if denied:
        return denied
    patient = _model(project, "Patient").objects.filter(patient_id=patient_id, project=project).first()
    if patient is None or not user_can_read_patient(request.user, patient):
        return _error("Patient not found in this project", 404)

    files = patient.files.select_related("modality").order_by("file_type", "created_at")
    files_data = [_file_ref(f) for f in files]
    return JsonResponse({
        "success": True,
        "project": _project_ref(project),
        "patient": {
            "patient_id": patient.patient_id,
            "name": patient.name,
            "uploaded_at": patient.uploaded_at.isoformat(),
        },
        "files": files_data,
        "total_files": len(files_data),
    })


def _patients_files(request, project):
    try:
        body = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return _error("Invalid JSON in request body", 400)
    patient_ids = body.get("patient_ids") if isinstance(body, dict) else None
    if not patient_ids:
        return _error("patient_ids list is required", 400)
    if not isinstance(patient_ids, list):
        return _error("patient_ids must be a list", 400)
    if len(patient_ids) > MAX_BULK_PATIENTS:
        return _error(f"Maximum {MAX_BULK_PATIENTS} patient IDs allowed per request", 400)

    patients = (
        _readable_patients(request, project)
        .filter(patient_id__in=patient_ids)
        .prefetch_related("files__modality")
        .order_by("patient_id")
    )
    patients_data = {}
    for patient in patients:
        files_data = [_file_ref(f) for f in patient.files.all()]
        patients_data[patient.patient_id] = {
            "patient_info": {
                "patient_id": patient.patient_id,
                "name": patient.name,
                "uploaded_at": patient.uploaded_at.isoformat(),
            },
            "files": files_data,
            "total_files": len(files_data),
        }
    return JsonResponse({
        "success": True,
        "project": _project_ref(project),
        "patients": patients_data,
        "found_patients": len(patients_data),
        "missing_patient_ids": [pid for pid in patient_ids if pid not in patients_data],
        "total_requested": len(patient_ids),
    })


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------


def _upload_target(request, project):
    """``(name, folder, None)`` from the form fields, else ``(.., .., response)``."""
    Patient = _model(project, "Patient")
    Folder = _model(project, "Folder")

    name = (request.POST.get("name") or "").strip()
    max_length = Patient._meta.get_field("name").max_length
    if len(name) > max_length:
        return None, None, _error(f"'name' is longer than {max_length} characters", 400)

    # The project is the URL's. A body naming another one is a client mix-up,
    # refused rather than silently overridden.
    claimed = request.POST.get("project")
    if claimed and claimed != str(project.id):
        return None, None, _error("'project' does not match the project in the URL", 400)

    folder_id = request.POST.get("folder") or ""
    if not folder_id.isdigit():
        return None, None, _error("'folder' is required: the id of a folder of this project", 400)
    folder = Folder.objects.filter(id=int(folder_id), project=project).first()
    if folder is None:
        return None, None, _error("'folder' is not a folder of this project", 400)
    return name, folder, None


def _sent_files(request, handler):
    """Field name -> uploaded files, with the handler's old names mapped to new."""
    files = {}
    for field_name in request.FILES:
        uploaded = request.FILES.getlist(field_name)
        if uploaded:
            files.setdefault(handler.aliases.get(field_name, field_name), []).extend(uploaded)
    return files


@require_http_methods(["POST"])
def project_upload(request, project_slug):
    """Create one patient in a folder of the project, with its files.

    Multipart: ``name``, ``folder`` (id), optional ``tags`` (comma-separated),
    and one part per file field. ``GET /api/projects/<slug>/`` lists the fields
    (``upload_fields``): a field the project does not accept is a 400 naming it,
    never a silently dropped file.
    """
    project, denied = _accessible_project(request, project_slug)
    if denied:
        return denied
    handler = get_handler(project.domain)
    if handler is None:
        return _error(f"Uploading to '{project.domain}' projects is not supported by the API", 501)

    name, folder, invalid = _upload_target(request, project)
    if invalid:
        return invalid
    if not user_can_write_annotations(request.user, folder, project):
        return _error("You do not have permission to upload into this folder", 403)

    accepted = handler.fields_for(project)
    accepted_names = {spec.name for spec, _ in accepted}
    companions = {c: spec.name for spec, _ in accepted for c in spec.companions}
    files = _sent_files(request, handler)
    if not files:
        return _error("Add at least one file", 400, accepted=sorted(accepted_names))
    rejected = sorted(n for n in files if n not in accepted_names and n not in companions)
    if rejected:
        return _error(
            "Fields not accepted by this project", 400,
            rejected=rejected, accepted=sorted(accepted_names),
        )
    orphaned = sorted(n for n, owner in companions.items() if n in files and owner not in files)
    if orphaned:
        return _error("Sent without the file they belong to", 400, rejected=orphaned)

    Tag = _model(project, "Tag")
    tag_names = _tag_names(request.POST.get("tags"))
    tag_max = Tag._meta.get_field("name").max_length
    if any(len(t) > tag_max for t in tag_names):
        return _error(f"Tags are at most {tag_max} characters", 400)

    Patient = _model(project, "Patient")
    try:
        handler.validate(files, accepted)
        result = UploadResult()
        # A failure past this point rolls the patient row back; bytes already
        # sent to object storage stay there, as with the web upload.
        with transaction.atomic():
            patient = Patient(name=name, project=project, folder=folder, uploaded_by=request.user)
            patient.save()
            if tag_names:
                patient.tags.set(_tags(Tag, tag_names))
            handler.save(patient, files, accepted, result)
    except UploadRejected as exc:
        return _error(str(exc), 400, **exc.extra)
    except ValidationError as exc:
        return _error("; ".join(exc.messages), 400)
    except Exception:
        logger.exception("Upload API failed for project %s", project.slug)
        return _error("Upload failed on the server; the patient was not created", 500)

    patient = Patient.objects.select_related("folder").prefetch_related("modalities", "tags").get(pk=patient.pk)
    return JsonResponse({
        "success": True,
        "message": "Patient uploaded successfully!",
        "patient": {
            **_patient_ref(patient),
            "project": _project_ref(project),
            "uploaded_by": {"id": request.user.id, "username": request.user.username},
            "tags": [tag.name for tag in patient.tags.all()],
            "upload_results": result.as_dict(),
        },
    })
