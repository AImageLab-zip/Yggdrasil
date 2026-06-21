"""Patient detail and management views."""
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.views.decorators.http import require_POST
import json
import os
import logging

from common.file_access import exists as artifact_exists
from common.permissions import (
    user_can_edit_caption,
    user_can_read_folder,
    user_can_view_caption_content,
    user_can_write_annotations,
    user_is_project_admin,
)

from .domain import get_domain_forms, get_domain_models, get_namespace
from .helpers import redirect_with_namespace, render_with_fallback
from ..file_utils import get_file_type_for_modality

logger = logging.getLogger(__name__)

@login_required
def patient_detail(request, patient_id):
    domain_models = get_domain_models(request)
    domain_forms = get_domain_forms(request)
    Patient = domain_models['Patient']
    Classification = domain_models['Classification']
    PatientManagementForm = domain_forms['PatientManagementForm']

    patient = get_object_or_404(Patient, patient_id=patient_id)
    user_profile = request.user.profile
    can_view = bool(patient.folder and user_can_read_folder(request.user, patient.folder, request))
    if user_is_project_admin(request.user, request):
        can_view = True

    if not can_view:
        messages.error(request, 'You do not have permission to view this scan.')
        return redirect_with_namespace(request, 'patient_list')
    
    ai_classification = patient.classifications.filter(classifier='pipeline').first()
    manual_classification = patient.classifications.filter(classifier='manual').first()
    
    management_form = PatientManagementForm(instance=patient, user=request.user)
    
    has_cbct = False
    try:
        raw_cbct = patient.get_cbct_raw_file()
        if raw_cbct and artifact_exists(raw_cbct.file_path):
            has_cbct = True
    except:
        pass

    has_uploaded_panoramic = False
    try:
        panoramic_candidates = list(
            patient.files.filter(modality__slug='panoramic').order_by('-created_at')
        )
        if not panoramic_candidates:
            panoramic_candidates = list(
                patient.files.filter(file_type__in=['panoramic_raw', 'panoramic_processed']).order_by('-created_at')
            )

        for panoramic_entry in panoramic_candidates:
            if panoramic_entry.file_path and artifact_exists(panoramic_entry.file_path):
                has_uploaded_panoramic = True
                break
    except Exception as e:
        logger.warning(f"Error checking uploaded panoramic availability: {e}")
    
    can_modify = bool(patient.folder and user_can_write_annotations(request.user, patient.folder, request))
    if user_is_project_admin(request.user, request):
        can_modify = True
    
    if request.method == 'POST' and can_modify:
        action = request.POST.get('action')
        
        if action == 'accept_ai' and ai_classification:
            Classification.objects.create(
                patient=patient,
                classifier='manual',
                sagittal_left=ai_classification.sagittal_left,
                sagittal_right=ai_classification.sagittal_right,
                vertical=ai_classification.vertical,
                transverse=ai_classification.transverse,
                midline=ai_classification.midline,
                annotator=request.user
            )
            messages.success(request, 'AI classification accepted!')
            return redirect_with_namespace(request, 'patient_detail', patient_id=patient_id)
        
        elif action == 'update_management':
            management_form = PatientManagementForm(request.POST, instance=patient, user=request.user)
            if management_form.is_valid():
                management_form.save()
                messages.success(request, 'Scan settings updated successfully!')
                return redirect_with_namespace(request, 'patient_detail', patient_id=patient_id)
        
        elif action == 'update_files':
            updated_files = []
            reprocess_ios = False
            reprocess_cbct = False
            
            has_upper_scan = 'upper_scan' in request.FILES
            has_lower_scan = 'lower_scan' in request.FILES
            has_cbct_file = 'cbct' in request.FILES
            has_cbct_folder = 'cbct_folder_files' in request.FILES
            
            if has_upper_scan:
                updated_files.append('upper scan')
                reprocess_ios = True
            
            if has_lower_scan:
                updated_files.append('lower scan')
                reprocess_ios = True
            
            if has_cbct_file:
                updated_files.append('CBCT')
                reprocess_cbct = True
            
            if has_cbct_folder:
                updated_files.append('CBCT Folder')
                reprocess_cbct = True
            
            if updated_files:
                from ..file_utils import save_cbct_to_dataset, save_ios_to_dataset
                
                if reprocess_ios and (has_upper_scan or has_lower_scan):
                    patient.classifications.filter(classifier='pipeline').delete()
                    patient.save()
                    
                    try:
                        result = save_ios_to_dataset(
                            patient,
                            request.FILES.get('upper_scan'),
                            request.FILES.get('lower_scan')
                        )
                        if result['processing_job']:
                            messages.success(request, f'IOS scan(s) uploaded and queued for processing (Job #{result["processing_job"].id})')
                        if result['bite_classification_job']:
                            messages.success(request, f'Bite classification job #{result["bite_classification_job"].id} created (waiting for IOS completion)')
                    except Exception as e:
                        messages.error(request, f'Error uploading IOS scan(s): {e}')
                
                if reprocess_cbct and (has_cbct_file or has_cbct_folder):
                    if has_cbct_folder:
                        try:
                            from ..file_utils import save_cbct_folder_to_dataset
                            from ..models import validate_cbct_folder
                            
                            cbct_folder_files = request.FILES.getlist('cbct_folder_files')
                            validate_cbct_folder(cbct_folder_files)
                            
                            folder_path, processing_job = save_cbct_folder_to_dataset(patient, cbct_folder_files)
                            messages.success(request, f'CBCT folder uploaded and queued for processing (Job #{processing_job.id})')
                        except Exception as e:
                            messages.error(request, f'Error uploading CBCT folder: {e}')
                    elif has_cbct_file:
                        try:
                            file_path, processing_job = save_cbct_to_dataset(patient, request.FILES['cbct'])
                            messages.success(request, f'CBCT uploaded and queued for processing (Job #{processing_job.id})')
                        except Exception as e:
                            messages.error(request, f'Error uploading CBCT: {e}')
                
                files_str = ', '.join(updated_files)
                messages.success(request, f'Successfully uploaded {files_str}! Files are queued for processing.')

                # Update patient modalities based on actual uploaded files using helper
                try:
                    from ..modality_helpers import get_modalities_for_uploaded_files
                    detected_modalities = get_modalities_for_uploaded_files(request.FILES)
                    if detected_modalities:
                        patient.modalities.add(*detected_modalities)
                except Exception as e:
                    logger.error(f"Error detecting modalities: {e}")
                return redirect_with_namespace(request, 'patient_detail', patient_id=patient_id)
            else:
                messages.warning(request, 'No files were selected for upload.')
                return redirect_with_namespace(request, 'patient_detail', patient_id=patient_id)
    
    # Build patient's modalities list (slug + name + subtypes) using relations and FileRegistry.modality only
    try:
        from common.models import Modality as _Modality
        # Start from relations
        rel_modalities = list(patient.modalities.all().order_by('name'))
        rel_by_slug = { (getattr(m, 'slug', None) or getattr(m, 'name', '').lower()): m for m in rel_modalities }
        # Add any modalities referenced by FileRegistry.modality
        file_mods = patient.files.filter(modality__isnull=False).values('modality__slug').distinct() if hasattr(patient, 'files') else []
        for fm in file_mods:
            slug = fm.get('modality__slug') or ''
            if slug and slug not in rel_by_slug:
                m = _Modality.objects.filter(slug=slug).first()
                if m:
                    rel_by_slug[slug] = m
        # Compose list with subtypes and UI label if present
        patient_modalities = []
        for slug, m in rel_by_slug.items():
            subtypes = []
            try:
                subtypes = list(getattr(m, 'subtypes', []) or [])
            except Exception:
                subtypes = []
            patient_modalities.append({
                'slug': getattr(m, 'slug', slug) or slug,
                'name': getattr(m, 'name', slug),
                'label': getattr(m, 'label', '') or '',
                'subtypes': subtypes,
            })
    except Exception:
        patient_modalities = []

    has_panoramic = has_uploaded_panoramic or has_cbct
    if not has_panoramic:
        patient_modalities = [m for m in patient_modalities if m.get('slug') != 'panoramic']

    has_intraoral_modality = any(
        (m.get('slug') in ['intraoral', 'intraoral-photo'])
        for m in patient_modalities
    )

    # Choose default modality: prefer first available (skip modalities marked as non-default)
    default_modality_slug = None
    try:
        from ..modality_helpers import get_modality_by_slug
        for m in patient_modalities:
            modality_obj = get_modality_by_slug(m['slug'])
            if modality_obj:
                metadata = getattr(modality_obj, 'metadata', {}) or {}
                # Skip if marked as non-default for viewing
                if not metadata.get('exclude_from_default_view', False):
                    default_modality_slug = m['slug']
                    break
    except Exception:
        # Fallback: just pick the first one
        if patient_modalities:
            default_modality_slug = patient_modalities[0]['slug']

    # JSON-serializable fields for template
    import json as _json
    patient_modalities_json = _json.dumps(patient_modalities)
    default_modality_json = _json.dumps(default_modality_slug)

    # Organize patient files for file management section
    patient_files = {'raw': [], 'processed': [], 'other': []}
    try:
        all_files = patient.files.all().order_by('-created_at')
        
        for file_obj in all_files:
            # Add computed properties for display
            modality_name = ''
            if file_obj.modality:
                modality_name = getattr(file_obj.modality, 'label', '') or getattr(file_obj.modality, 'name', '') or ''
            elif file_obj.metadata and file_obj.metadata.get('modality_slug'):
                # Fallback to trying to get modality info from metadata
                try:
                    from common.models import Modality as _Modality
                    mod = _Modality.objects.filter(slug=file_obj.metadata['modality_slug']).first()
                    if mod:
                        modality_name = getattr(mod, 'label', '') or getattr(mod, 'name', '') or ''
                except Exception:
                    pass
            
            file_data = {
                'id': file_obj.id,
                'file_type': file_obj.file_type,
                'file_path': file_obj.file_path,
                'file_size': file_obj.file_size,
                'created_at': file_obj.created_at,
                'filename': os.path.basename(file_obj.file_path) if file_obj.file_path else 'Unknown',
                'original_filename': file_obj.metadata.get('original_filename', '') if file_obj.metadata else '',
                'file_size_mb': f"{file_obj.file_size / (1024 * 1024):.2f}" if file_obj.file_size else '0.00',
                'modality_name': modality_name,
            }
            
            # Categorize files dynamically based on file_type
            # Check for raw files (contains _raw or is rgb_image)
            if '_raw' in file_obj.file_type or file_obj.file_type == 'rgb_image':
                patient_files['raw'].append(file_data)
            # Check for processed files (contains _processed or is bite_classification)
            elif '_processed' in file_obj.file_type or file_obj.file_type == 'bite_classification':
                patient_files['processed'].append(file_data)
            else:
                patient_files['other'].append(file_data)
    except Exception as e:
        logger.error(f"Error organizing patient files: {e}")


    # Voice captions
    # Non-admin users can see caption metadata for all captions. Caption content
    # access depends on the user's folder role.
    voice_captions = patient.voice_captions.all()
    is_admin_user = user_is_project_admin(request.user, request)
    can_create_caption = bool(
        is_admin_user
        or (patient.folder and user_can_write_annotations(request.user, patient.folder, request))
    )
    for caption in voice_captions:
        caption.can_view_content = user_can_view_caption_content(request.user, caption, request)
        caption.can_edit_content = user_can_edit_caption(request.user, caption)
        caption.is_ghost = not caption.can_view_content

    # Build modality files lookup for drag-drop grid
    modality_files = {}
    try:
        for m in patient_modalities:
            slug = m.get('slug', '')
            if slug:
                # Find the FileRegistry entry for this modality
                from common.models import Modality as _Modality
                modality_obj = _Modality.objects.filter(slug=slug).first()
                if modality_obj:
                    files_qs = patient.files.filter(modality=modality_obj)

                    if slug == 'cbct':
                        # Prefer processed CBCT entries that expose a valid NIfTI volume.
                        file_obj = None
                        processed_candidates = files_qs.filter(file_type='cbct_processed').order_by('-created_at')
                        for processed_entry in processed_candidates:
                            if processed_entry.file_hash == 'multi-file' and processed_entry.metadata:
                                files_data = processed_entry.metadata.get('files', {})
                                volume_data = files_data.get('volume_nifti', {}) if isinstance(files_data, dict) else {}
                                volume_path = volume_data.get('path') if isinstance(volume_data, dict) else None
                                if volume_path and artifact_exists(volume_path):
                                    file_obj = processed_entry
                                    break
                            elif processed_entry.file_path and (
                                processed_entry.file_path.endswith('.nii') or processed_entry.file_path.endswith('.nii.gz')
                            ):
                                file_obj = processed_entry
                                break

                        if not file_obj:
                            raw_candidates = files_qs.filter(file_type='cbct_raw').order_by('-created_at')
                            for raw_entry in raw_candidates:
                                if raw_entry.file_path and (
                                    raw_entry.file_path.endswith('.nii') or raw_entry.file_path.endswith('.nii.gz')
                                ):
                                    file_obj = raw_entry
                                    break
                    else:
                        file_obj = files_qs.order_by('-created_at').first()

                    if file_obj:
                        modality_files[slug] = {
                            'id': file_obj.id,
                            'file_type': file_obj.file_type,
                        }
    except Exception as e:
        logger.warning(f"Error building modality_files: {e}")

    # JSON-serialize modality_files for template
    modality_files_json = _json.dumps(modality_files)

    context = {
        'patient': patient,
        'ai_classification': ai_classification,
        'manual_classification': manual_classification,
        'user_profile': user_profile,
        'management_form': management_form,
        'has_cbct': has_cbct,
        'has_panoramic': has_panoramic,
        'has_intraoral_modality': has_intraoral_modality,
        'can_modify_segmentation': can_modify,
        'patient_modalities': patient_modalities,
        'default_modality_slug': default_modality_slug,
        'patient_modalities_json': patient_modalities_json,
        'default_modality_json': default_modality_json,
        'patient_files': patient_files,
        'voice_captions': voice_captions,
        'is_admin_user': is_admin_user,
        'can_create_caption': can_create_caption,
        'modality_files': modality_files,
        'modality_files_json': modality_files_json,
    }
    # Allowed modalities for current project (to conditionally show upload controls)
    try:
        allowed_modalities = []
        cp_id = request.session.get('current_project_id')
        if cp_id:
            from common.models import Project as _Project
            proj = _Project.objects.prefetch_related('modalities').get(id=cp_id)
            allowed_modalities = list(proj.modalities.filter(is_active=True))
        if not allowed_modalities:
            # Fallback: get all active modalities
            from common.models import Modality as _Modality
            allowed_modalities = list(_Modality.objects.filter(is_active=True))

        raw_file_type_options = []
        seen_raw_types = set()
        valid_file_types = set()
        try:
            from common.models import FileRegistry as _FileRegistry

            valid_file_types = set(_FileRegistry.get_file_type_choices_dict().keys())
        except Exception:
            valid_file_types = set()

        for modality in allowed_modalities:
            slug = (getattr(modality, 'slug', '') or '').strip()
            if not slug:
                continue

            display_name = (
                (getattr(modality, 'label', '') or '').strip()
                or (getattr(modality, 'name', '') or '').strip()
                or slug.upper()
            )

            subtype_values = [s for s in (getattr(modality, 'subtypes', None) or []) if str(s).strip()]
            if slug == 'ios' and not subtype_values:
                subtype_values = ['upper', 'lower']

            candidates = []
            if subtype_values:
                for subtype in subtype_values:
                    raw_type = get_file_type_for_modality(slug, is_processed=False, subtype=str(subtype).strip())
                    subtype_label = str(subtype).replace('_', ' ').title()
                    candidates.append((raw_type, f"{display_name} {subtype_label}"))
            else:
                raw_type = get_file_type_for_modality(slug, is_processed=False)
                candidates.append((raw_type, display_name))

            for raw_type, label in candidates:
                if not raw_type or '_raw' not in raw_type:
                    continue
                if valid_file_types and raw_type not in valid_file_types:
                    continue
                if raw_type in seen_raw_types:
                    continue
                seen_raw_types.add(raw_type)
                raw_file_type_options.append({'value': raw_type, 'label': label})

        context['allowed_modalities'] = allowed_modalities
        context['allowed_modality_slugs'] = [m.slug for m in allowed_modalities]
        context['raw_file_type_options'] = raw_file_type_options
    except Exception:
        pass
    try:
        from django.db.models import Case, When, IntegerField as _IntegerField
        from django.urls import reverse as _reverse
        ns = get_namespace(request)
        video_file = patient.files.filter(
            file_type__in=['video_processed', 'video_raw']
        ).annotate(
            _prio=Case(
                When(file_type='video_processed', subtype='compressed', then=0),
                When(file_type='video_processed', then=1),
                default=2,
                output_field=_IntegerField(),
            )
        ).order_by('_prio', '-created_at').first()
        if video_file:
            context['video_file'] = video_file
            context['video_url'] = _reverse(f'{ns}:api_serve_file', kwargs={'file_id': video_file.id})
        context['has_video'] = bool(video_file)
        subsampled_file = patient.files.filter(
            file_type='video_processed', subtype='subsampled'
        ).order_by('-created_at').first()
        worker_source_file = subsampled_file or video_file
        if subsampled_file:
            context['subsampled_video_url'] = _reverse(f'{ns}:api_serve_file', kwargs={'file_id': subsampled_file.id})
        if worker_source_file and getattr(worker_source_file, 'file_path', None):
            context['worker_video_source_ref'] = worker_source_file.file_path
            context['worker_video_source_file_id'] = worker_source_file.id
    except Exception:
        context['has_video'] = False
        context['video_url'] = None
        context['worker_video_source_ref'] = None
        context['worker_video_source_file_id'] = None
    return render_with_fallback(request, 'patient_detail', context)

@login_required
@require_POST
def update_patient_name(request, patient_id):
    """AJAX endpoint for updating scan name"""
    user_profile = request.user.profile
    Patient = get_domain_models(request)['Patient']
    
    try:
        patient = get_object_or_404(Patient, patient_id=patient_id)
        
        can_modify = bool(patient.folder and user_can_write_annotations(request.user, patient.folder, request))
        if user_is_project_admin(request.user, request):
            can_modify = True
        
        if not can_modify:
            return JsonResponse({'error': 'Permission denied'}, status=403)
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON payload'}, status=400)
        
        new_name = data.get('name', '').strip()
        if not new_name:
            return JsonResponse({'error': 'Name cannot be empty'}, status=400)
        if len(new_name) > 100:
            return JsonResponse({'error': 'Name must be 100 characters or fewer'}, status=400)
        
        patient.name = new_name
        patient.save()
        
        return JsonResponse({'success': True, 'name': new_name})
        
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
