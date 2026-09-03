"""Patient detail and management views."""
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.views.decorators.http import require_POST
import json
import os
import logging
import hashlib

from common.file_access import exists as artifact_exists
from common.object_storage import get_object_storage
from common.annotation_lock import annotation_lock_reasons, lock_message
from common.permissions import (
    project_allows_annotation,
    user_can_edit_caption,
    user_can_read_patient,
    user_can_view_caption_content,
    user_can_write_patient_annotations,
    user_is_project_admin,
)

from .domain import get_domain_forms, get_domain_models, get_namespace
from .panoramic_state import current_browser_panoramic
from .helpers import redirect_with_namespace, render_with_fallback
from ..file_utils import get_file_type_for_modality

logger = logging.getLogger(__name__)


def _bundle_output_path(file_obj, output_key):
    if file_obj.subtype == output_key and artifact_exists(file_obj.file_path):
        return file_obj.file_path, 'primary'
    metadata = file_obj.metadata if isinstance(file_obj.metadata, dict) else {}
    files = metadata.get('files', {})
    output = files.get(output_key, {}) if isinstance(files, dict) else {}
    path = output.get('path') if isinstance(output, dict) else None
    if path and artifact_exists(path):
        return path, output_key
    return None, None


def _bundle_output_hash(file_obj, file_key):
    metadata = file_obj.metadata if isinstance(file_obj.metadata, dict) else {}
    files = metadata.get('files', {})
    output = files.get(file_key, {}) if isinstance(files, dict) else {}
    nested_hash = ''
    if isinstance(output, dict):
        for name in ('sha256', 'file_hash', 'hash'):
            if output.get(name):
                nested_hash = str(output[name])
                break
    if file_key and file_key != 'primary' and isinstance(output, dict):
        # Legacy multi-file rows often have the constant hash "multi-file".
        # Include the selected artifact path so changing bundle metadata invalidates
        # any panoramic state bound to the previous object.
        object_identity = ''
        path = str(output.get('path') or '')
        if path and not nested_hash:
            try:
                object_identity = str(get_object_storage().head(path).etag or '')
            except Exception:
                # Existing bundle metadata may predate object ETags. The path still
                # protects against metadata retargeting when storage is unavailable.
                object_identity = ''
        token = '\0'.join((
            str(file_obj.file_hash or ''),
            str(file_key),
            path,
            nested_hash,
            object_identity,
        ))
        return hashlib.sha256(token.encode('utf-8')).hexdigest()
    if nested_hash:
        return nested_hash
    return file_obj.file_hash


def _usable_raw_volumes(raw_files):
    """Raw rows the viewer can actually display, keyed by the path a job names.

    A raw volume is one ``.nii.gz`` object, so its existence is a ``head()``. Rows
    that are object-storage *prefixes* rather than objects (folder uploads) are not
    display volumes and are left out: ``head()`` on a prefix raises, which is finding
    F13, and the extension test is what keeps them out before that can happen.
    """
    return {
        file_obj.file_path: file_obj
        for file_obj in raw_files
        if file_obj.file_path
        and file_obj.file_path.endswith(('.nii', '.nii.gz'))
        and artifact_exists(file_obj.file_path)
    }


def _resolved_cbct_viewer_source(patient):
    """Resolve the exact volume/segmentation pair used by the CBCT viewer."""
    processed = list(
        patient.files.filter(
            file_type='cbct_processed',
            processing_job__status='completed',
        )
        .select_related('processing_job')
        .order_by('-processing_job__completed_at', '-created_at', '-id')
    )
    raw_files = list(
        patient.files.filter(file_type='cbct_raw').order_by('-created_at', '-id')
    )
    raw_by_path = _usable_raw_volumes(raw_files)

    jobs = []
    for file_obj in processed:
        if file_obj.processing_job and file_obj.processing_job not in jobs:
            jobs.append(file_obj.processing_job)

    for job in jobs:
        job_rows = [row for row in processed if row.processing_job_id == job.id]
        segmentation_row = None
        segmentation_key = None
        for row in job_rows:
            path, file_key = _bundle_output_path(row, 'segmentation_nifti')
            if path:
                segmentation_row, segmentation_key = row, file_key
                break
        if not segmentation_row:
            continue

        display_row = None
        display_key = None
        for row in job_rows:
            path, file_key = _bundle_output_path(row, 'volume_nifti')
            if path:
                display_row, display_key = row, file_key
                break

        if not display_row:
            raw_input = (job.input_files or {}).get('input')
            display_row = raw_by_path.get(raw_input)
            display_key = 'primary' if display_row else None
        if not display_row:
            continue

        output_spec = (job.output_files or {}).get('segmentation_nifti')
        label_max = output_spec.get('label_max', 98) if isinstance(output_spec, dict) else 98
        return {
            'job': job,
            'file': display_row,
            'file_key': display_key,
            'file_hash': _bundle_output_hash(display_row, display_key),
            'segmentation_file': segmentation_row,
            'segmentation_key': segmentation_key,
            'segmentation_hash': _bundle_output_hash(segmentation_row, segmentation_key),
            'segmentation': {
                'id': segmentation_row.id,
                'fileKey': segmentation_key,
                'labelMax': label_max,
            },
        }

    if raw_by_path:
        display_row = next(row for row in raw_files if row.file_path in raw_by_path)
        return {
            'job': None,
            'file': display_row,
            'file_key': 'primary',
            'file_hash': display_row.file_hash,
            'segmentation_file': None,
            'segmentation_key': '',
            'segmentation_hash': '',
            'segmentation': None,
        }

    for row in processed:
        path, file_key = _bundle_output_path(row, 'volume_nifti')
        if path:
            return {
                'job': row.processing_job,
                'file': row,
                'file_key': file_key,
                'file_hash': _bundle_output_hash(row, file_key),
                'segmentation_file': None,
                'segmentation_key': '',
                'segmentation_hash': '',
                'segmentation': None,
            }
    return None


def _cbct_viewer_files(patient):
    """Return a job-paired display volume and optional segmentation."""
    source = _resolved_cbct_viewer_source(patient)
    if not source:
        return None, None, None
    return source['file'], source['file_key'], source['segmentation']


def _panorex_source_data(patient, source):
    """The payload the panoramic editor reads: the active CBCT pair, plus any stored arch.

    The seven-field source comparison this function used to carry is now
    :func:`~maxillo.views.panoramic_state.current_browser_panoramic`, which three call
    sites share. An arch that does not describe the active pair is reported as absent
    rather than as stale -- the editor's contract is "revision 0 means start again".
    """
    if not source:
        return None

    current = current_browser_panoramic(patient, source)
    arch = current["arch"] if current["matchesSource"] else None
    state_data = None
    if arch:
        state_data = {
            'revision': current["revision"],
            'generationUuid': current["generationUuid"],
            'axialSlice': arch["axial_slice"],
            'volumeShape': arch["volume_shape"],
            'spline': arch["spline"],
            'geometrySource': arch["geometry_source"],
            'defaultMode': arch["default_mode"],
            'algorithmVersion': arch["algorithm_version"],
        }
    segmentation_file = source['segmentation_file']
    return {
        'jobId': source['job'].id if source['job'] else None,
        'volumeFileId': source['file'].id,
        'volumeFileKey': source['file_key'],
        'volumeFileHash': source['file_hash'],
        'fileId': source['file'].id,
        'fileKey': source['file_key'],
        'fileHash': source['file_hash'],
        'segmentationFileId': segmentation_file.id if segmentation_file else None,
        'segmentationFileKey': source['segmentation_key'] or None,
        'segmentationFileHash': source['segmentation_hash'] or None,
        'revision': current["effectiveRevision"],
        'state': state_data,
    }


def _call_patient_flag(patient, name):
    value = getattr(patient, name, False)
    return bool(value() if callable(value) else value)

@login_required
def patient_detail(request, patient_id):
    domain_models = get_domain_models(request)
    domain_forms = get_domain_forms(request)
    Patient = domain_models['Patient']
    Classification = domain_models['Classification']
    PatientManagementForm = domain_forms['PatientManagementForm']

    patient = get_object_or_404(Patient, patient_id=patient_id)
    user_profile = request.user.profile
    can_view = bool(patient.project and user_can_read_patient(request.user, patient))
    if user_is_project_admin(request.user, patient.project):
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
    
    can_modify = bool(patient.project and user_can_write_patient_annotations(request.user, patient))
    if user_is_project_admin(request.user, patient.project):
        can_modify = True
    
    if request.method == 'POST' and can_modify:
        action = request.POST.get('action')
        
        if action == 'accept_ai' and ai_classification:
            if not (
                project_allows_annotation(patient, 'classification')
                or project_allows_annotation(patient, 'bite_classification')
            ):
                messages.error(request, 'Occlusion classification is disabled for this project.')
                return redirect_with_namespace(request, 'patient_detail', patient_id=patient_id)
            # update_or_create, not create: one manual classification per patient is
            # a database constraint, and accepting the AI result twice -- or accepting
            # it after a human already classified by hand -- is an ordinary thing for a
            # user to do, not an error to raise at them.
            Classification.objects.update_or_create(
                patient=patient,
                classifier='manual',
                defaults={
                    'sagittal_left': ai_classification.sagittal_left,
                    'sagittal_right': ai_classification.sagittal_right,
                    'vertical': ai_classification.vertical,
                    'transverse': ai_classification.transverse,
                    'midline': ai_classification.midline,
                    'annotator': request.user,
                },
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
            
            if has_upper_scan:
                updated_files.append('upper scan')
                reprocess_ios = True
            
            if has_lower_scan:
                updated_files.append('lower scan')
                reprocess_ios = True
            
            if has_cbct_file:
                updated_files.append('CBCT')
                reprocess_cbct = True
            
            if updated_files:
                from ..file_utils import save_cbct_to_dataset, save_ios_to_dataset
                queued_processing = False
                
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
                            queued_processing = True
                            messages.success(request, f'IOS scan(s) uploaded and queued for processing (Job #{result["processing_job"].id})')
                        if result['bite_classification_job']:
                            queued_processing = True
                            messages.success(request, f'Bite classification job #{result["bite_classification_job"].id} created (waiting for IOS completion)')
                    except Exception as e:
                        messages.error(request, f'Error uploading IOS scan(s): {e}')
                
                if reprocess_cbct:
                    try:
                        file_path, processing_job = save_cbct_to_dataset(patient, request.FILES['cbct'])
                        if processing_job:
                            queued_processing = True
                            messages.success(request, f'CBCT uploaded and queued for processing (Job #{processing_job.id})')
                        else:
                            messages.success(request, 'CBCT uploaded successfully')
                    except Exception as e:
                        messages.error(request, f'Error uploading CBCT: {e}')
                
                files_str = ', '.join(updated_files)
                if queued_processing:
                    messages.success(request, f'Successfully uploaded {files_str}! Files are queued for processing.')
                else:
                    messages.success(request, f'Successfully uploaded {files_str}!')

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
    
    # One fetch of the patient's registry rows, shared by the modality presence
    # filter below and the file-management section further down.
    try:
        patient_file_rows = list(
            patient.files.select_related('modality').order_by('-created_at')
        )
    except Exception as e:
        logger.error(f"Error loading patient files: {e}")
        patient_file_rows = []

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

    # Project scoping: only show modalities the patient's project enables
    # (patients cannot carry modalities outside their project's set).
    try:
        if getattr(patient, 'project', None) is not None:
            _allowed_mod_slugs = set(
                patient.project.modalities.filter(is_active=True).values_list('slug', flat=True)
            )
            if _allowed_mod_slugs:
                patient_modalities = [
                    m for m in patient_modalities if m.get('slug') in _allowed_mod_slugs
                ]
    except Exception:
        pass

    # Annotation methods enabled by the patient's project; the templates hide
    # annotation tools whose method is not enabled (server endpoints re-check).
    allowed_annotations = []
    try:
        if getattr(patient, 'project', None) is not None:
            allowed_annotations = list(
                patient.project.annotation_methods
                .filter(is_active=True)
                .values_list('slug', flat=True)
            )
    except Exception:
        allowed_annotations = []

    # Sidebar tabs: occlusion and captions panes are hidden when the project
    # does not enable their annotation methods; the first visible tab is active.
    occlusion_enabled = bool(
        set(allowed_annotations)
        & {'classification', 'bite_classification', 'intraoral_segmentation'}
    )
    captions_enabled = 'voice_caption' in allowed_annotations
    if occlusion_enabled:
        default_tab = 'classification'
    elif captions_enabled:
        default_tab = 'captions'
    else:
        default_tab = 'files'

    has_panoramic = has_uploaded_panoramic or has_cbct

    # Only offer a viewer tab for a modality the patient actually has data for.
    # `patient.modalities` is a *declaration* -- set at upload from the detected
    # types, and never pruned -- so on its own it offered tabs onto panes that
    # could only answer "No IOS Scans". Presence is read from the files, through
    # the same visibility gate the serve endpoint applies.
    #
    # Panoramic keeps its extra allowance: it is derivable from CBCT in the
    # browser (`cbct_to_panoramic`), so a CBCT-only patient can open the editor
    # and generate one, and file presence alone would take that away.
    try:
        from common.modality_config import present_modality_slugs

        present_slugs = present_modality_slugs(patient_file_rows)
        if has_panoramic:
            present_slugs.add('panoramic')
        patient_modalities = [
            m for m in patient_modalities if m.get('slug') in present_slugs
        ]
    except Exception as e:
        logger.error(f"Error filtering modalities by presence: {e}")
        if not has_panoramic:
            patient_modalities = [
                m for m in patient_modalities if m.get('slug') != 'panoramic'
            ]

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


    # Organize patient files for file management section
    patient_files = {'raw': [], 'processed': [], 'other': []}
    try:
        all_files = patient_file_rows
        
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

            if (
                file_obj.file_type == 'cbct_processed'
                and file_obj.file_hash == 'multi-file'
                and isinstance(file_obj.metadata, dict)
            ):
                bundle_files = []
                files_meta = file_obj.metadata.get('files', {})
                if isinstance(files_meta, dict):
                    primary_bundle = files_meta.get('segmentation_nifti')
                    if isinstance(primary_bundle, dict) and primary_bundle.get('path'):
                        file_data['filename'] = os.path.basename(primary_bundle.get('path', ''))

                    bundle_labels = {
                        'segmentation_nifti': 'Segmentation NIfTI',
                    }
                    bundle_order = ['segmentation_nifti']
                    bundle_keys = [k for k in bundle_order if k in files_meta]
                    bundle_keys.extend(k for k in files_meta.keys() if k not in bundle_keys)
                    for bundle_key in bundle_keys:
                        bundle_meta = files_meta[bundle_key]
                        if bundle_key != 'segmentation_nifti':
                            continue
                        if not isinstance(bundle_meta, dict) or not bundle_meta.get('path'):
                            continue
                        bundle_files.append({
                            'key': bundle_key,
                            'label': bundle_labels.get(bundle_key, bundle_key.replace('_', ' ').title()),
                            'filename': os.path.basename(bundle_meta.get('path', '')),
                        })
                file_data['bundle_files'] = bundle_files
            
            # Categorize files dynamically based on file_type
            # Check for raw files (contains _raw or is rgb_image)
            if '_raw' in file_obj.file_type or file_obj.file_type == 'rgb_image':
                # Security gate: hide raw inputs that are discarded, or blocked
                # until processing produces a processed output.
                from common.modality_config import raw_file_hidden
                if raw_file_hidden(file_obj):
                    continue
                patient_files['raw'].append(file_data)
            # Check for processed files (contains _processed or is bite_classification)
            elif '_processed' in file_obj.file_type or file_obj.file_type == 'bite_classification':
                patient_files['processed'].append(file_data)
            else:
                patient_files['other'].append(file_data)
    except Exception as e:
        logger.error(f"Error organizing patient files: {e}")


    # Voice captions
    # Viewers and admins see all captions; annotators see only their own (to
    # avoid bias during annotation).
    # Through the canonical helper, which resolves the role from the patient's
    # *project*. This used to read it from `patient.folder`, so an unfiled
    # patient -- `Patient.folder` is SET_NULL, and folders come and go -- had no
    # role at all, and its captions were hidden from the project's own viewers.
    voice_captions = patient.voice_captions.all()
    is_admin_user = user_is_project_admin(request.user, patient.project)
    for caption in voice_captions:
        caption.can_view_content = user_can_view_caption_content(request.user, caption)
        caption.can_edit_content = user_can_edit_caption(request.user, caption)
        caption.is_ghost = not caption.can_view_content
    can_create_caption = can_modify

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
                        cbct_source = _resolved_cbct_viewer_source(patient)
                        if cbct_source:
                            file_obj = cbct_source['file']
                            file_key = cbct_source['file_key']
                            cbct_segmentation = cbct_source['segmentation']
                        else:
                            file_obj, file_key, cbct_segmentation = None, None, None
                    else:
                        file_obj = files_qs.order_by('-created_at').first()
                        file_key = 'primary'

                    if file_obj:
                        entry = {
                            'id': file_obj.id,
                            'file_type': file_obj.file_type,
                            'file_key': file_key,
                        }
                        modality_files[slug] = entry
    except Exception as e:
        logger.warning(f"Error building modality_files: {e}")

    # Structured payloads rendered via |json_script (XSS-safe, no |safe needed)
    django_data = {
        'canEdit': bool(can_modify),
        'scanId': patient.patient_id,
        'hasIOS': _call_patient_flag(patient, 'has_ios_scans'),
        'hasCBCT': bool(has_cbct),
        # Whether a panoramic image exists at all, so the viewer can show its empty
        # state without asking the API. Every CBCT patient offers the panoramic pane
        # (a panoramic can be generated from the volume), so the pane used to open by
        # fetching `?meta=1` for patients that have nothing yet -- an answer the server
        # can only give as a 404, which the browser logs as a failed request before any
        # of our code sees it. The page already knows.
        'hasPanoramicImage': bool(has_uploaded_panoramic),
        'isCBCTProcessed': _call_patient_flag(patient, 'is_cbct_processed'),
        'modalities': patient_modalities,
        'defaultModality': default_modality_slug,
    }
    viewer_grid_data = {
        'scanId': patient.patient_id,
        'projectNamespace': (request.resolver_match.namespace if request.resolver_match else None) or 'maxillo',
        'modalityFiles': modality_files,
        'segmentationFile': locals().get('cbct_segmentation'),
        'panorexSource': _panorex_source_data(patient, locals().get('cbct_source')),
        'fixedMode': True,
        'enableDragDrop': False,
        'enableContextMenu': True,
        'allowClearWindow': False,
    }
    # The photo stack's payload. Deliberately just the endpoint and the ids: the images
    # themselves are listed by the modality endpoint the surface already has, and
    # duplicating that list into the page would give a stale copy of it a second life.
    _namespace = (request.resolver_match.namespace if request.resolver_match else None) or 'maxillo'
    photo_stack_data = {
        'patientId': patient.patient_id,
        'projectNamespace': _namespace,
        'modalitySlug': 'teleradiography',
        'endpoint': f'/{_namespace}/api/patient/{patient.patient_id}/teleradiography/?meta=1',
    }
    # The intraoral photographs are the same surface plus tooth segmentation, so the
    # payload is the same shape with `segmentation` switched on. `canModify` is carried
    # because the editor disables its own controls for a reader; the server refuses the
    # write regardless, and this only avoids offering an action that would then fail.
    intraoral_stack_data = {
        'patientId': patient.patient_id,
        'projectNamespace': _namespace,
        'modalitySlug': 'intraoral-photo',
        'endpoint': f'/{_namespace}/api/patient/{patient.patient_id}/intraoral/',
        'segmentation': True,
        'canModify': bool(can_modify),
    }

    # The IOS mesh surface's payload. Deliberately the endpoints and the ids, not the
    # scan URLs: the `/data/` endpoint the surface already has is what lists them, and a
    # copy embedded in the page would be a stale one the moment a scan is re-uploaded.
    # `canModify` is carried because the workbench disables its own controls for a reader;
    # the server refuses the write regardless, and this only avoids offering an action
    # that would then fail.
    mesh_landmark_data = {
        'patientId': patient.patient_id,
        'projectNamespace': _namespace,
        'modalitySlug': 'ios',
        'meshEndpoint': f'/{_namespace}/api/patient/{patient.patient_id}/data/',
        'landmarkEndpoint': f'/{_namespace}/api/patients/{patient.patient_id}/ios-landmarks/',
        'canModify': bool(can_modify),
    }

    # Processing steps possible for this patient's rerun ("Rerun" header action).
    try:
        from common.modality_config import rerunnable_steps_for_patient
        _rerunnable = rerunnable_steps_for_patient(patient_file_rows, [], patient=patient)
        rerunnable_step_slugs = [step['slug'] for step in _rerunnable]
    except Exception:
        logger.warning("Failed to compute rerunnable steps for patient %s", patient.patient_id, exc_info=True)
        rerunnable_step_slugs = [
            m.get('slug') for m in patient_modalities if m.get('slug') != 'rawzip'
        ]

    # Raw inputs freeze once annotation work exists; the panoramic freezes on
    # everything except its own state (see common.annotation_lock).
    raw_lock_reasons = annotation_lock_reasons(patient)
    panoramic_lock_reasons = annotation_lock_reasons(patient, include_panoramic=False)

    context = {
        'patient': patient,
        'photo_stack_data': photo_stack_data,
        'mesh_landmark_data': mesh_landmark_data,
        'intraoral_stack_data': intraoral_stack_data,
        'raw_data_locked': bool(raw_lock_reasons),
        'raw_lock_message': lock_message(raw_lock_reasons),
        'panoramic_locked': bool(panoramic_lock_reasons),
        'ai_classification': ai_classification,
        'manual_classification': manual_classification,
        'user_profile': user_profile,
        'management_form': management_form,
        'has_cbct': has_cbct,
        'has_panoramic': has_panoramic,
        'can_modify_segmentation': can_modify,
        'patient_modalities': patient_modalities,
        'default_modality_slug': default_modality_slug,
        'allowed_annotations': allowed_annotations,
        'occlusion_enabled': occlusion_enabled,
        'captions_enabled': captions_enabled,
        'default_tab': default_tab,
        'django_data': django_data,
        'patient_files': patient_files,
        'voice_captions': voice_captions,
        'is_admin_user': is_admin_user,
        'can_create_caption': can_create_caption,
        'modality_files': modality_files,
        'rerunnable_step_slugs': rerunnable_step_slugs,
        'viewer_grid_data': viewer_grid_data,
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
        video_candidates = list(patient.files.filter(
            file_type__in=['video_processed', 'video_raw']
        ).annotate(
            _prio=Case(
                When(file_type='video_processed', subtype='compressed', then=0),
                When(file_type='video_processed', then=1),
                default=2,
                output_field=_IntegerField(),
            )
        ).order_by('_prio', '-created_at'))
        # The best file we can *describe* wins over the best file, because the
        # annotator needs a recorded ffprobe result and there is no point preferring
        # a compressed derivative nobody has probed over a raw file somebody has.
        # Falling back to the top-ranked row keeps plain playback working either way;
        # only the annotator is withheld, and `video_state` says why.
        video_file = _first_probed_video(video_candidates) or (
            video_candidates[0] if video_candidates else None
        )
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
        # **Playback and annotation are two different files, on purpose.** Playback takes
        # the best thing there is, raw included. Annotation takes the subsampled track and
        # nothing else -- see `_video_annotate_payload`, which now carries both so the one
        # surface on the page can watch the first and annotate the second.
        context['video_annotate_data'] = _video_annotate_payload(
            request, ns, patient, subsampled_file, video_file
        )
        context['video_state'] = _video_state(
            ns, video_file, subsampled_file, context['video_annotate_data']
        )
        context['video_diagnosis'] = _video_diagnosis(patient, video_candidates, video_file)
    except Exception:
        # Narrow on purpose. This used to be the widest `except` on the page and it
        # answered every failure inside it -- a mistyped URL name included, see the
        # comment in `_video_annotate_payload` -- with "this patient has no video",
        # which is a *claim about the data* made on the strength of a bug in the view.
        # It still must not take the rest of the patient record down, so it still
        # catches; what changed is that it now says so where someone will see it.
        logger.exception(
            "Could not build the video context for patient %s; the page will render "
            "without a viewer.", patient.patient_id,
        )
        context['has_video'] = False
        context['video_url'] = None
        context['worker_video_source_ref'] = None
        context['worker_video_source_file_id'] = None
        context['video_annotate_data'] = 'null'
        context['video_state'] = 'error'
        context['video_diagnosis'] = ''

    # Record for the landing "Continue where you left off" strip (best-effort).
    from common.activity import record_recent
    _ns = request.resolver_match.namespace if request.resolver_match else 'maxillo'
    record_recent(
        request.user, _ns, patient.patient_id,
        patient_name=getattr(patient, 'name', '') or '',
        project_label=(context.get('current_project_name') or _ns).title(),
    )
    return render_with_fallback(request, 'patient_detail', context)

@login_required
@require_POST
def update_patient_name(request, patient_id):
    """AJAX endpoint for updating scan name"""
    user_profile = request.user.profile
    Patient = get_domain_models(request)['Patient']
    
    try:
        patient = get_object_or_404(Patient, patient_id=patient_id)
        
        can_modify = bool(patient.project and user_can_write_patient_annotations(request.user, patient))
        if user_is_project_admin(request.user, patient.project):
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


def _first_probed_video(candidates):
    """The highest-ranked video row that carries a recorded probe, or ``None``.

    ``annotations_rasterize_video_masks`` records a probe on the **``video_raw``**
    row, while this page ranks a ``video_processed`` / ``compressed`` derivative
    first. Asking only the top-ranked row therefore reported "no probe" on studies
    that had one, which the surface renders as "No video uploaded for this patient."
    """
    from laparoscopy import video_probe

    for candidate in candidates:
        if video_probe.recorded_probe(candidate) is not None:
            return candidate
    return None


def _video_state(namespace, video_file, subsampled_file, annotate_data):
    """Why the video surface looks the way it does, for the template to say out loud.

    ``absent`` -- there is no video row at all. ``processing`` -- the recording is
    uploaded and plays, but the job that makes the annotation track has not produced it
    yet, so there is nothing to annotate *on*. ``unprobed`` -- the track exists but
    nothing has recorded its frame rate, so the annotator declines rather than guess
    (run ``manage.py laparoscopy_probe_videos``). ``ready`` -- the annotator has what it
    needs and will mount.

    Separate sentences because the page had one for all of them, and a stored, playable
    recording being described as "not uploaded" sends people looking for a lost file
    instead of waiting for a job or running a command.

    **Derived from the payload, not from the namespace.** The first version answered
    ``ready`` whenever the namespace was not ``laparoscopy``, which was a way of saying
    "this surface is not on this page" -- but the template reads this to choose its
    sentence, and ``ready`` with a ``null`` payload means the placeholder stays on
    screen still claiming no video was uploaded. ``ready`` means exactly one thing: the
    payload is real and the annotator will mount from it.
    """
    if not video_file:
        return 'absent'
    if annotate_data and annotate_data != 'null':
        return 'ready'
    if subsampled_file is None:
        return 'processing'
    return 'unprobed'


def _video_diagnosis(patient, candidates, video_file):
    """One line naming what the server looked for and what it found, for staff.

    "No video uploaded for this patient." over a file somebody can see in object
    storage is a claim, and the page had no way to show its working. This is that
    working, rendered only for an administrator.

    It answers the two questions that actually distinguish the cases, because a
    ``FileRegistry`` row can exist and still not be found here: it might carry a
    ``file_type`` this page does not look for, or it might be attached to the wrong
    patient FK -- a video registered against ``patient`` instead of
    ``laparoscopy_patient`` is invisible to ``patient.files`` while sitting in the
    bucket exactly as expected.
    """
    if video_file is not None:
        from laparoscopy import video_probe

        rows = ', '.join(
            f"#{row.id} {row.file_type}"
            + (f"/{row.subtype}" if row.subtype else '')
            + ('' if video_probe.recorded_probe(row) else ' (no probe)')
            for row in candidates
        )
        return f"Video rows for this patient: {rows}. Playing #{video_file.id}."

    types = sorted({row.file_type for row in patient.files.all()})
    if not types:
        return "This patient has no registered files at all."
    return (
        "No video_raw or video_processed row is registered against this patient. "
        f"It has {len(types)} other file type(s): {', '.join(types)}. "
        "A video in object storage that is not registered here, or registered "
        "against a different patient, will not be found."
    )


def _video_annotate_payload(request, namespace, patient, video_file, playback_file=None):
    """The JSON the Phase 10 annotator mounts from, or ``'null'``.

    ``null`` is a real answer and the surface handles it: the bootstrap reports that it
    declined and mounts nothing. The caller turns it into a ``video_state`` the template
    has a sentence for, because the placeholder left on screen used to read "No video
    uploaded for this patient." over a stored, playable recording.

    **``video_file`` here is the subsampled track, not whatever plays best.** A raw
    laparoscopy recording runs at 25-30 fps and the annotator draws a labelmap per
    *annotated* frame, so opening it on the raw video offers thirty times more frames
    than anyone can annotate and produces a record whose frames no export can line up
    with: `laparoscopy/export_processor.py` reads the subsampled track, and the two would
    be describing different films. So annotation waits for the derivative the video job
    produces -- one sharpest frame per source second -- and the page says it is
    processing until then. Playback is unaffected and still takes the best file there is.

    **``playback_file`` is the film to watch, and it is a different one.** The subsampled
    track is literally one frame per second: pressing play on it steps through stills, and
    "the video plays the cut frames" is the accurate description of watching a 1 fps film.
    So the payload carries both -- for patient 15 the subsampled track probes at 1 fps /
    187 frames and the compressed one at 30 fps / 5608 frames, over the same 187 seconds
    of surgery -- and the surface watches the compressed film while continuing to file
    every mask against a subsampled frame. The two are addressable by the same clock,
    which is what makes the pair safe: stopping at 42.7 s lands on subsampled frame 43,
    the frame the export writes an NPZ for. Nothing about the record changes.

    ``None`` (or the annotation track itself) means there is no second film, and the
    surface watches the one it annotates -- which is what every non-laparoscopy caller and
    any study without a compressed derivative gets.

    The other withholding is a track with **no recorded probe**: ``fps`` is a property of
    the file that a browser cannot read, and ``laparoscopy/video_probe.py`` caches it
    when the file arrives. Without it the viewer would have to guess a frame rate, and
    guessing wrong puts every mask on the wrong frame while looking entirely correct.
    """
    import json

    from django.urls import reverse as _reverse

    from laparoscopy import video_probe

    if video_file is None or namespace != 'laparoscopy':
        return 'null'
    probe = video_probe.recorded_probe(video_file)
    if probe is None:
        logger.info(
            "Patient %s has a video with no recorded probe; the annotator is not "
            "mounted. Run `manage.py laparoscopy_probe_videos --patient %s`.",
            patient.patient_id, patient.patient_id,
        )
        return 'null'
    playback_id = getattr(playback_file, 'id', None)
    return json.dumps(
        {
            'patientId': patient.patient_id,
            'videoUrl': _reverse(
                f'{namespace}:api_serve_file', kwargs={'file_id': video_file.id}
            ),
            'playbackUrl': (
                _reverse(f'{namespace}:api_serve_file', kwargs={'file_id': playback_id})
                if playback_id and playback_id != video_file.id
                else None
            ),
            # Unnamespaced: laparoscopy/urls.py is included without a namespace, and
            # the `laparoscopy:` prefix belongs to the maxillo app urls it re-includes.
            # Getting this wrong raised inside the view's broad `except`, which turned a
            # bad URL name into "this patient has no video" -- a page that renders and
            # is simply missing its viewer.
            'endpoint': _reverse(
                'patient_video_annotations',
                kwargs={'patient_id': patient.patient_id},
            ),
            'width': int(probe['width']),
            'height': int(probe['height']),
            'fps': float(probe['fps']),
            'frameCount': int(probe['frame_count']),
        }
    )
