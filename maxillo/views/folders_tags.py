"""Folder and tag management views.

Folders live inside Projects; folder creation is scoped to the session's
current project, and folder "permissions" are really project access
(ProjectAccess). The legacy FolderAccess rows are untouched.
"""
from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import JsonResponse
from django.views.decorators.http import require_POST, require_http_methods
import json
import logging

from common.models import ProjectAccess
from .domain import get_domain_models, get_namespace
from common.permissions import (
    user_can_write_patient_annotations,
    user_is_project_admin,
)

logger = logging.getLogger(__name__)


def _current_project(request):
    from common.models import Project
    pid = request.session.get('current_project_id')
    if not pid:
        return None
    try:
        return Project.objects.get(id=pid, is_active=True)
    except Project.DoesNotExist:
        return None


@login_required
@require_POST
def create_folder(request):
    """Create a folder inside the current project (single-level only)."""
    Folder = get_domain_models(request)['Folder']
    try:
        if not user_is_project_admin(request.user, request):
            return JsonResponse({'error': 'Permission denied'}, status=403)
        project = _current_project(request)
        if project is None:
            return JsonResponse({'error': 'No project selected'}, status=400)
        data = json.loads(request.body) if request.body else request.POST
        name = (data.get('name') or '').strip()
        if not name:
            return JsonResponse({'error': 'Folder name is required'}, status=400)
        folder, created = Folder.objects.get_or_create(
            name=name, parent=None, project=project,
            defaults={'created_by': request.user},
        )
        return JsonResponse({'success': True, 'folder': {'id': folder.id, 'name': folder.name, 'path': folder.name, 'created': created}})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
@require_http_methods(["GET"])
def folder_stats(request, folder_id):
    if not user_is_project_admin(request.user, request):
        return JsonResponse({'error': 'Permission denied'}, status=403)

    domain_models = get_domain_models(request)
    Folder = domain_models['Folder']
    Patient = domain_models['Patient']

    folder = get_object_or_404(Folder, id=folder_id)
    patient_count = Patient.objects.filter(folder=folder).count()
    return JsonResponse({
        'success': True,
        'folder': {'id': folder.id, 'name': folder.name},
        'stats': {'patient_count': patient_count},
    })


@login_required
@require_http_methods(["GET"])
def folder_permissions(request, folder_id):
    """List ProjectAccess rows for the folder's project (roles viewer/annotator/admin)."""
    if not user_is_project_admin(request.user, request):
        return JsonResponse({'error': 'Permission denied'}, status=403)

    Folder = get_domain_models(request)['Folder']
    folder = get_object_or_404(Folder, id=folder_id)
    project = folder.project
    rows = ProjectAccess.objects.filter(project=project).select_related('user').order_by('user__username')
    users = User.objects.filter(is_active=True).order_by('username').values('id', 'username', 'email')
    return JsonResponse({
        'success': True,
        'folder': {'id': folder.id, 'name': folder.name},
        'permissions': [
            {'user_id': row.user_id, 'username': row.user.username, 'role': row.role}
            for row in rows
        ],
        'users': list(users),
        'roles': ['viewer', 'annotator', 'admin'],
    })


@login_required
@require_http_methods(["POST"])
def upsert_folder_permission(request, folder_id):
    """Grant/update a user's ProjectAccess on the folder's project."""
    if not user_is_project_admin(request.user, request):
        return JsonResponse({'error': 'Permission denied'}, status=403)

    Folder = get_domain_models(request)['Folder']
    folder = get_object_or_404(Folder, id=folder_id)
    project = folder.project

    data = json.loads(request.body) if request.body else request.POST
    user_id = data.get('user_id')
    role = (data.get('role') or '').strip()
    if role not in {'viewer', 'annotator', 'admin'}:
        return JsonResponse({'error': 'Invalid role'}, status=400)
    if not user_id:
        return JsonResponse({'error': 'user_id required'}, status=400)

    user = get_object_or_404(User, id=user_id)
    row, _ = ProjectAccess.objects.update_or_create(
        user=user,
        project=project,
        defaults={'role': role},
    )
    return JsonResponse({'success': True, 'user_id': row.user_id, 'role': row.role})


@login_required
@require_http_methods(["DELETE"])
def delete_folder_permission(request, folder_id, user_id):
    if not user_is_project_admin(request.user, request):
        return JsonResponse({'error': 'Permission denied'}, status=403)

    Folder = get_domain_models(request)['Folder']
    folder = get_object_or_404(Folder, id=folder_id)
    ProjectAccess.objects.filter(project=folder.project, user_id=user_id).delete()
    return JsonResponse({'success': True})


@login_required
@require_http_methods(["POST"])
def rename_folder(request, folder_id):
    if not user_is_project_admin(request.user, request):
        return JsonResponse({'error': 'Permission denied'}, status=403)

    Folder = get_domain_models(request)['Folder']
    folder = get_object_or_404(Folder, id=folder_id)
    data = json.loads(request.body) if request.body else request.POST
    name = (data.get('name') or '').strip()
    if not name:
        return JsonResponse({'error': 'Folder name is required'}, status=400)
    folder.name = name
    folder.parent = None
    folder.save(update_fields=['name', 'parent'])
    return JsonResponse({'success': True, 'folder': {'id': folder.id, 'name': folder.name}})


@login_required
@require_POST
def move_patients_to_folder(request):
    """Bulk move scans to a folder (patients stay in the folder's project)."""
    domain_models = get_domain_models(request)
    Patient = domain_models['Patient']
    Folder = domain_models['Folder']
    try:
        if not user_is_project_admin(request.user, request):
            return JsonResponse({'error': 'Permission denied'}, status=403)
        data = json.loads(request.body) if request.body else request.POST
        scan_ids = data.get('scan_ids', [])
        folder_id = data.get('folder_id')
        if not isinstance(scan_ids, list) or not scan_ids:
            return JsonResponse({'error': 'scan_ids list is required'}, status=400)
        if not folder_id or folder_id in ('root', 'all'):
            return JsonResponse({'error': 'A folder is required (patients must live in a project folder)'}, status=400)
        folder = get_object_or_404(Folder, id=folder_id)
        # Moving also sets the project, so cross-project moves are explicit.
        updated = Patient.objects.filter(patient_id__in=scan_ids).update(
            folder=folder, project=folder.project
        )
        return JsonResponse({'success': True, 'updated': updated})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
@require_POST
def add_patient_tag(request, patient_id):
    """Add a tag to a scan; creates tag if it doesn't exist."""
    domain_models = get_domain_models(request)
    Patient = domain_models['Patient']
    Tag = domain_models['Tag']
    try:
        patient = get_object_or_404(Patient, patient_id=patient_id)
        if not user_can_write_patient_annotations(request.user, patient):
            return JsonResponse({'error': 'Permission denied'}, status=403)
        data = json.loads(request.body) if request.body else request.POST
        tag_name = (data.get('tag') or '').strip()
        if not tag_name:
            return JsonResponse({'error': 'Tag name required'}, status=400)
        tag, _ = Tag.objects.get_or_create(name=tag_name)
        patient.tags.add(tag)
        return JsonResponse({'success': True, 'tags': patient.tag_names()})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
@require_POST
def remove_patient_tag(request, patient_id):
    """Remove a tag from a scan by tag name or id."""
    domain_models = get_domain_models(request)
    Patient = domain_models['Patient']
    Tag = domain_models['Tag']
    try:
        patient = get_object_or_404(Patient, patient_id=patient_id)
        if not user_can_write_patient_annotations(request.user, patient):
            return JsonResponse({'error': 'Permission denied'}, status=403)
        data = json.loads(request.body) if request.body else request.POST
        tag_name = (data.get('tag') or '').strip()
        tag_id = data.get('tag_id')
        tag = None
        if tag_id:
            tag = get_object_or_404(Tag, id=tag_id)
        elif tag_name:
            tag = Tag.objects.filter(name__iexact=tag_name).first()
        if not tag:
            return JsonResponse({'error': 'Tag not found'}, status=404)
        patient.tags.remove(tag)
        return JsonResponse({'success': True, 'tags': patient.tag_names()})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
