"""Patient list and project selection views."""
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.paginator import Paginator
from django.apps import apps
from django.db.models import Q

from ..models import Patient as MaxilloPatient, Folder as MaxilloFolder, Tag as MaxilloTag
from .helpers import render_with_fallback
from common.models import Project, ProjectAccess
from common.permissions import (
    filter_folders_for_user,
    filter_patients_for_user,
    user_is_project_admin,
)
import logging
logger = logging.getLogger(__name__)


def _get_domain_models(request):
    ns = (getattr(request, 'resolver_match', None) and request.resolver_match.namespace) or ''
    if ns == 'brain':
        return (
            apps.get_model('brain', 'Patient'),
            apps.get_model('brain', 'Folder'),
            apps.get_model('brain', 'Tag'),
        )
    return MaxilloPatient, MaxilloFolder, MaxilloTag

def home(request):
    # Show project selection for authenticated users
    if request.user.is_authenticated:
        # If no projects yet, bootstrap defaults
        all_projects = Project.objects.filter(is_active=True)
        
        # Admins can see all projects
        if request.user.is_staff:
            projects = all_projects.order_by('name')
        else:
            accessible_project_ids = ProjectAccess.objects.filter(
                user=request.user
            ).values_list('project_id', flat=True)
            projects = all_projects.filter(id__in=accessible_project_ids).order_by('name')

        current_project_id = request.session.get('current_project_id')
        current_project_name = None
        if current_project_id:
            cp = projects.filter(id=current_project_id).first()
            if cp:
                current_project_name = cp.name
        continue_url = None
        if current_project_name:
            # Build continue URL based on project slug
            project = projects.filter(id=current_project_id).first()
            if project and project.slug:
                continue_url = f'/{project.slug}/'
        
        return render(request, 'common/landing.html', {
            'projects': projects,
            'current_project_id': current_project_id,
            'current_project_name': current_project_name,
            'continue_url': continue_url,
        })
    return render(request, 'common/landing.html')


@login_required
def select_project(request, project_id: int):
    project = get_object_or_404(Project, id=project_id, is_active=True)
    
    # Check if user has access to this project
    if not user_is_project_admin(request.user, project):
        has_access = ProjectAccess.objects.filter(
            user=request.user,
            project=project
        ).exists()
        if not has_access:
            messages.error(request, f"You don't have access to the {project.name} project.")
            return redirect('home')
    
    request.session['current_project_id'] = project.id
    messages.success(request, f"Project set to {project.name}")
    return redirect('patient_list')


@login_required
def patient_list(request):
    namespace = (getattr(request, 'resolver_match', None) and request.resolver_match.namespace) or 'maxillo'
    is_admin = user_is_project_admin(request.user, namespace)
    Patient, Folder, Tag = _get_domain_models(request)
    
    # Import Job model early for use in prefetch
    try:
        from common.models import Job as _Job
    except Exception:
        _Job = None
    
    select_related_fields = ['dataset', 'uploaded_by', 'folder']
    if any(field.name == 'project' for field in Patient._meta.fields):
        select_related_fields.insert(0, 'project')

    prefetch_fields = [
        'classifications',
        'voice_captions',
        'voice_captions__user',
        'tags',
        'modalities',
    ]
    has_files_relation = any(rel.name == 'files' for rel in Patient._meta.related_objects)
    if has_files_relation:
        prefetch_fields.extend(['files', 'files__modality'])

    base_queryset = Patient.objects.select_related(*select_related_fields).prefetch_related(*prefetch_fields)
    
    # Prefetch jobs for all patients if Job model is available
    if _Job is not None and any(rel.name == 'jobs' for rel in Patient._meta.related_objects):
        base_queryset = base_queryset.prefetch_related('jobs')
    
    # Enforce project access: admins see all; others require ProjectAccess entry
    current_project_id = request.session.get('current_project_id')
    has_access = True
    if not is_admin and current_project_id:
        has_access = ProjectAccess.objects.filter(user=request.user, project_id=current_project_id).exists()
        if not has_access:
            messages.error(request, 'You are not allowed to access this project.')
            return redirect('home')

    patients = filter_patients_for_user(request.user, base_queryset, namespace)
    
    # Filter by app namespace if mounted under /maxillo or /brain
    if current_project_id and any(field.name == 'project' for field in Patient._meta.fields):
        patients = patients.filter(project_id=current_project_id)

    # Get filter parameters
    search_query = request.GET.get('search', '').strip()
    has_ios_filter = request.GET.get('has_ios', '')
    has_cbct_filter = request.GET.get('has_cbct', '')
    has_bite_filter = request.GET.get('has_bite', '')
    has_voice_filter = request.GET.get('has_voice', '')

    folder_id = request.GET.get('folder')
    tags_selected = request.GET.getlist('tags')
    if not tags_selected:
        comma = request.GET.get('tags', '')
        if comma:
            tags_selected = [t.strip() for t in comma.split(',') if t.strip()]
    per_page = int(request.GET.get('per_page', 20))
    
    # Store base queryset for folder counts BEFORE applying folder filter
    base_patients_for_counts = patients
    
    if search_query:
        patients = patients.filter(
            Q(name__icontains=search_query) |
            Q(patient_id__icontains=search_query)
        )
    
    if folder_id and folder_id != 'all':
        if folder_id == 'root':
            patients = patients.filter(folder__isnull=True)
        else:
            try:
                patients = patients.filter(folder_id=int(folder_id))
            except ValueError:
                pass
    
    if tags_selected:
        patients = patients.filter(tags__name__in=tags_selected).distinct()
    
    patients = patients.order_by('-uploaded_at')
    
    # Get filter parameters for optimization decision
    per_page = int(request.GET.get('per_page', 20))
    page_number = request.GET.get('page')
    
    # Check if we have modality status filters that require processing all patients
    # Build dynamic status filters from query params
    has_status_filters = False
    allowed_modalities = []
    status_filters = {}
    try:
        if current_project_id:
            proj = Project.objects.prefetch_related('modalities').get(id=current_project_id)
            allowed_modalities = list(proj.modalities.filter(is_active=True))
            for m in allowed_modalities:
                slug = getattr(m, 'slug', '') or ''
                if slug:
                    if slug == 'rawzip':
                        continue
                    val = request.GET.get(f'status_{slug}', '').strip()
                    if val:
                        status_filters[slug] = val
                        has_status_filters = True
    except Exception:
        pass
    
    # PERFORMANCE OPTIMIZATION:
    # If no status filters are active, we only process patients on the current page (fast path)
    # If status filters are active, we need to process all patients first (slow path)
    if not has_status_filters:
        # Fast path: Use Paginator to slice the queryset, then process only current page
        temp_paginator = Paginator(patients, per_page)
        temp_page = temp_paginator.get_page(page_number)
        patients_to_process = temp_page.object_list
        # Store page info for later
        fast_path_page_info = {
            'num_pages': temp_paginator.num_pages,
            'count': temp_paginator.count,
            'number': temp_page.number,
            'has_next': temp_page.has_next(),
            'has_previous': temp_page.has_previous(),
            'has_other_pages': temp_page.has_other_pages(),
        }
    else:
        # Slow path: need to process all patients for filtering
        patients_to_process = patients
        fast_path_page_info = None
    
    # Build patient data efficiently using prefetched data
    patients_with_status = []
    for patient in patients_to_process:
        # Get classifications from prefetched data
        classifications = list(patient.classifications.all())
        manual_classification = next((c for c in classifications if c.classifier == 'manual'), None)
        ai_classification = next((c for c in classifications if c.classifier == 'pipeline'), None)
        
        # Get voice captions from prefetched data
        voice_captions = list(patient.voice_captions.all())
        voice_caption_processing = any(
            vc.processing_status in ['pending', 'processing'] for vc in voice_captions
        )
        voice_caption_processed = (
            voice_captions and 
            all(vc.processing_status == 'completed' for vc in voice_captions)
        )
        
        # Get voice annotators from prefetched data
        voice_annotators = list(set(vc.user.username for vc in voice_captions))
        
        # Derive available modalities dynamically from relation and FileRegistry.modality
        # Use prefetched data to avoid N+1 queries
        try:
            rel_modalities = list(patient.modalities.all())
        except Exception:
            rel_modalities = []
        rel_by_slug = { (m.slug or m.name.lower()): m for m in rel_modalities }
        
        # Add modalities referenced by files (using prefetched data)
        patient_files = []
        try:
            # Use prefetched files instead of querying database
            patient_files = list(patient.files.all())
            seen_modality_slugs = set()
            for file_obj in patient_files:
                if file_obj.modality and file_obj.modality.slug:
                    slug = file_obj.modality.slug
                    if slug not in rel_by_slug and slug not in seen_modality_slugs:
                        seen_modality_slugs.add(slug)
                        # Create a lightweight placeholder object
                        class _M: pass
                        _m = _M()
                        _m.slug = slug
                        _m.name = file_obj.modality.name or slug
                        _m.icon = file_obj.modality.icon or ''
                        _m.label = file_obj.modality.label or ''
                        rel_by_slug[slug] = _m
        except Exception:
            pass

        available_modality_objs = list(rel_by_slug.values())

        # Compute per-modality status using Jobs and presence of any files linked by modality
        # Use prefetched data to avoid N+1 queries
        modality_status_list = []
        
        # Get prefetched jobs for this patient
        try:
            patient_jobs = list(patient.jobs.all()) if hasattr(patient, 'jobs') else []
        except Exception:
            patient_jobs = []
        
        # Build job status lookup by modality_slug for O(1) lookups
        jobs_by_modality = {}
        for job in patient_jobs:
            slug = getattr(job, 'modality_slug', None)
            if slug:
                if slug not in jobs_by_modality:
                    jobs_by_modality[slug] = []
                jobs_by_modality[slug].append(job)
        
        # Build file lookup by modality slug using already-fetched patient_files
        files_by_modality = {}
        for f in patient_files:
            if f.modality and f.modality.slug:
                slug = f.modality.slug
                if slug not in files_by_modality:
                    files_by_modality[slug] = []
                files_by_modality[slug].append(f)
        
        # Process ALL allowed modalities for the project (not just patient's modalities)
        # This ensures all modalities are shown, with absent ones displayed in grey
        for m in allowed_modalities:
            slug = getattr(m, 'slug', '') or ''
            name = getattr(m, 'name', slug)
            icon = getattr(m, 'icon', '') or ''
            label = getattr(m, 'label', '') or ''
            if slug == 'rawzip' or slug == 'voice':
                continue
            
            status = 'absent'
            has_any_files = slug in files_by_modality and len(files_by_modality[slug]) > 0
            
            # Determine status precedence: failed > processing > pending > processed > absent
            # Check jobs using prefetched data
            modality_jobs = jobs_by_modality.get(slug, [])
            if any(job.status == 'failed' for job in modality_jobs):
                status = 'failed'
            elif any(job.status == 'processing' for job in modality_jobs):
                status = 'processing'
            elif any(job.status in ['pending', 'retrying'] for job in modality_jobs):
                status = 'pending'
            elif has_any_files:
                status = 'processed'

            modality_status_list.append({'slug': slug, 'name': name, 'icon': icon, 'label': label, 'status': status})

        patient_data = {
            'patient': patient,
            'manual_classification': manual_classification,
            'ai_classification': ai_classification,
            'has_manual': manual_classification is not None,
            'has_ai_only': ai_classification is not None and manual_classification is None,
            'needs_processing': manual_classification is None and ai_classification is None,
            'voice_caption_processing': voice_caption_processing,
            'voice_caption_processed': voice_caption_processed,
            'voice_caption_count': len(voice_captions),
            'voice_annotators': voice_annotators,
            'tags': patient.tag_names(),
            'folder': patient.folder,
            # Deprecated keys kept for backward-compat (not used in template after dynamic change)
            'available_modalities': [m.slug for m in available_modality_objs],
            'modality_statuses': {ms['slug']: ms['status'] for ms in modality_status_list},
            'modality_status_list': modality_status_list,
        }
        patients_with_status.append(patient_data)
    
    folders = Folder.objects.filter(parent__isnull=True).order_by('name')
    folders = filter_folders_for_user(request.user, folders, namespace)
    
    # Add patient counts for each folder
    folders_with_counts = []
    for folder in folders:
        # Count patients in this folder, respecting the same visibility and project filters
        # Use base_patients_for_counts to avoid folder filter being applied
        folder_patients = base_patients_for_counts.filter(folder=folder)
        folder_count = folder_patients.count()
        folders_with_counts.append({
            'folder': folder,
            'patient_count': folder_count
        })
    
    all_tags = Tag.objects.all().order_by('name')

    # Compose filter specs for template rendering (allowed_modalities and status_filters already built above)
    modality_filter_specs = []
    for m in allowed_modalities:
        slug = getattr(m, 'slug', '') or ''
        if slug == 'rawzip':
            continue
        name = getattr(m, 'name', slug)
        icon = getattr(m, 'icon', '') or ''
        label = getattr(m, 'label', '') or ''
        modality_filter_specs.append({
            'slug': slug,
            'name': name,
            'icon': icon,
            'label': label,
            'value': status_filters.get(slug, ''),
        })

    # Apply dynamic status filters if active (slow path only)
    if has_status_filters:
        filtered = []
        for item in patients_with_status:
            modality_status_by_slug = { ms['slug']: ms['status'] for ms in item.get('modality_status_list', []) }
            # Voice status is computed from dedicated fields if voice is among allowed
            if 'voice' in status_filters:
                voice_status = 'absent'
                # Use prefetched job data instead of querying
                patient_jobs = []
                try:
                    if hasattr(item['patient'], 'jobs'):
                        patient_jobs = list(item['patient'].jobs.all())
                except Exception:
                    pass
                
                if any(job.modality_slug == 'voice' and job.status == 'failed' for job in patient_jobs):
                    voice_status = 'failed'
                elif item.get('voice_caption_processing'):
                    voice_status = 'processing'
                elif item.get('voice_caption_processed'):
                    voice_status = 'processed'
                modality_status_by_slug['voice'] = voice_status
            passes = True
            for slug, desired in status_filters.items():
                if not desired:
                    continue
                actual = modality_status_by_slug.get(slug, 'absent')
                if actual != desired:
                    passes = False
                    break
            if passes:
                filtered.append(item)
        patients_with_status = filtered
    
    # Create final page_obj with processed patient data
    if fast_path_page_info:
        # Fast path: Reconstruct page object with our processed data
        from django.core.paginator import Page
        # Create a dummy paginator with the correct count
        dummy_list = list(range(fast_path_page_info['count']))
        paginator = Paginator(dummy_list, per_page)
        # Create the page manually
        page_obj = Page(patients_with_status, fast_path_page_info['number'], paginator)
    else:
        # Slow path: Standard pagination on filtered results
        paginator = Paginator(patients_with_status, per_page)
        page_obj = paginator.get_page(page_number)
    
    context = {
        'page_obj': page_obj,
        'current_project_id': current_project_id,
        'search_query': search_query,
        'has_ios_filter': has_ios_filter,
        'has_cbct_filter': has_cbct_filter,
        'has_bite_filter': has_bite_filter,
        'has_voice_filter': has_voice_filter,
        'folder_id': folder_id or 'all',
        'selected_tags': tags_selected,
        'folders': folders_with_counts,
        'all_tags': all_tags,
        'per_page': per_page,
        'user_profile': request.user.profile,
        'is_admin_user': is_admin,
        'allowed_modalities': allowed_modalities,
        'status_filters': status_filters,
        'modality_filter_specs': modality_filter_specs,
    }
    # Prefer app-specific template via fallback helper
    return render_with_fallback(request, 'patient_list', context)
