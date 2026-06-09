"""Project-based API endpoints."""
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.db.models import Prefetch
from django.apps import apps
import json
import os
import logging
import traceback
from common.models import Project, Modality, ProjectAccess, FileRegistry
from common.permissions import (
    PermissionChecker,
    filter_folders_for_user,
    filter_patients_for_user,
    user_can_read_folder,
    user_can_write_annotations,
    user_is_project_admin,
)

logger = logging.getLogger(__name__)

CBCT_SLUG = 'cbct'
IOS_SLUG = 'ios'
INTROARAL_PHOTO_SLUG = 'intraoral-photo'
PANORAMIC_SLUG = 'panoramic'
TELERADIOGRAPHY_SLUG = 'teleradiography'
RAWZIP_SLUG = 'rawzip'


def _project_domain(project_slug):
    return 'maxillo'


def _project_models(project_slug):
    return {
        'Patient': apps.get_model('maxillo', 'Patient'),
        'Folder': apps.get_model('maxillo', 'Folder'),
        'Tag': apps.get_model('maxillo', 'Tag'),
    }


def _upload_form_class(project_slug):
    from ..forms import PatientUploadForm
    return PatientUploadForm

@csrf_exempt
@require_http_methods(["POST"])
def project_upload_api(request, project_slug):
    """
    API endpoint to upload patient data with all supported modalities
    URL: /api/<project_slug>/upload/
    Accepts multipart/form-data with all the same fields as the normal upload form
    Returns the created patient data as JSON
    """
    try:
        domain = _project_domain(project_slug)
        models_map = _project_models(project_slug)
        Patient = models_map['Patient']

        # Check if project exists
        try:
            project = Project.objects.get(slug=project_slug)
        except Project.DoesNotExist:
            return JsonResponse({'error': 'Project not found'}, status=404)
        
        # Check user permissions
        if not request.user.is_authenticated:
            return JsonResponse({'error': 'Authentication required'}, status=401)

        if not ProjectAccess.objects.filter(user=request.user, project=project).exists():
            return JsonResponse({'error': 'You do not have permission to upload scans'}, status=403)
        
        # Use the existing form logic from upload_scan view
        PatientUploadForm = _upload_form_class(project_slug)
        patient_upload_form = PatientUploadForm(request.POST, request.FILES, user=request.user)
        
        # Check for CBCT folder upload (not supported)
        cbct_upload_type = request.POST.get('cbct_upload_type', 'file')
        if cbct_upload_type == 'folder' and request.FILES.getlist('cbct_folder_files'):
            return JsonResponse({'error': 'CBCT folder uploads has been temporarily disabled.'}, status=400)
        
        if not patient_upload_form.is_valid():
            return JsonResponse({
                'error': 'Form validation failed',
                'form_errors': patient_upload_form.errors
            }, status=400)
        
        # Create patient
        patient = patient_upload_form.save(commit=False)
        patient.uploaded_by = request.user
        
        # Handle folder assignment
        folder = patient_upload_form.cleaned_data.get('folder')
        if folder:
            patient.folder = folder

        if not user_is_project_admin(request.user, project):
            if not folder or not user_can_write_annotations(request.user, folder, _project_domain(project_slug)):
                return JsonResponse({'error': 'You do not have permission to upload into this folder'}, status=403)
        
        patient.save()
        
        # Infer modalities from uploaded file field names using helper
        from ..modality_helpers import infer_modality_from_field_name, get_modalities_for_uploaded_files
        candidate_slugs = ['ios', 'intraoral-photo']
        
        # Infer from uploaded files
        for field_name in request.FILES.keys():
            inferred = infer_modality_from_field_name(field_name)
            if inferred and inferred not in candidate_slugs:
                candidate_slugs.append(inferred)
        
        # Get allowed modalities for this project
        allowed_slugs = set(project.modalities.values_list('slug', flat=True))
        logger.error(f"Allowed slugs: {allowed_slugs}")
        logger.error(f"Candidate slugs: {candidate_slugs}")
        # Add modalities to patient
        if candidate_slugs:
            try:
                from common.models import Modality as _Modality
                from django.utils.text import slugify as _slugify
                for slug in candidate_slugs:
                    m = _Modality.objects.filter(slug=slug).first()
                    if not m:
                        m = _Modality.objects.filter(name__iexact=slug).first()
                    if not m:
                        m = _Modality.objects.filter(slug=_slugify(slug)).first()
                    if m and (not allowed_slugs or m.slug in allowed_slugs):
                        patient.modalities.add(m)
                        logger.error(f"Added modality {m.slug} to patient {patient.patient_id}")
            except Exception:
                pass
        
        # Process file uploads and create jobs (reuse existing logic)
        upload_results = {'messages': [], 'jobs': []}
        
        # Handle IOS files
        try:
            upper_file = request.FILES.get('upper_scan_raw')
            lower_file = request.FILES.get('lower_scan_raw')
            if upper_file and lower_file:
                from ..file_utils import save_ios_to_dataset
                ios_result = save_ios_to_dataset(patient, upper_file, lower_file)
                if ios_result.get('processing_job'):
                    upload_results['jobs'].append({
                        'id': ios_result['processing_job'].id,
                        'type': 'ios',
                        'status': ios_result['processing_job'].status
                    })
                    upload_results['messages'].append(f"IOS scan(s) queued for processing")
                if ios_result.get('bite_classification_job'):
                    upload_results['jobs'].append({
                        'id': ios_result['bite_classification_job'].id,
                        'type': 'bite_classification',
                        'status': ios_result['bite_classification_job'].status
                    })
        except Exception as e:
            upload_results['messages'].append(f"Error creating IOS processing job: {e}")
        
        # Handle CBCT files
        try:
            cbct_file = request.FILES.get('cbct')
            if cbct_file:
                from ..file_utils import save_cbct_to_dataset
                file_path, processing_job = save_cbct_to_dataset(patient, cbct_file)
                if processing_job:
                    upload_results['jobs'].append({
                        'id': processing_job.id,
                        'type': 'cbct',
                        'status': processing_job.status
                    })
                    upload_results['messages'].append(f"CBCT scan queued for processing")
        except Exception as e:
            upload_results['messages'].append(f"Error creating CBCT processing job: {e}\ntraceback: {traceback.format_exc()}")

        # Handle intraoral photographs
        try:
            intraoral_files = request.FILES.getlist('intraoral_photos')
            if intraoral_files:
                from ..file_utils import save_intraoral_photos_to_dataset
                saved_entries, errors, job = save_intraoral_photos_to_dataset(patient, intraoral_files)
                if saved_entries:
                    upload_results['messages'].append(f"Uploaded {len(saved_entries)} intraoral image(s)")
                if errors:
                    upload_results['messages'].extend(errors)
                if job:
                    upload_results['jobs'].append({
                        'id': job.id,
                        'type': 'intraoral-photo',
                        'status': job.status
                    })
        except Exception as e:
            upload_results['messages'].append(f"Error creating intraoral processing job: {e}")
        
        # Handle Teleradiography and Panoramic
        try:
            teleradiography_file = request.FILES.get('teleradiography')
            if teleradiography_file:
                from ..file_utils import save_generic_modality_file
                fr, job = save_generic_modality_file(patient, 'teleradiography', teleradiography_file)
                if fr:
                    upload_results['messages'].append(f"Teleradiography uploaded successfully")
                if job:
                    upload_results['jobs'].append({
                        'id': job.id,
                        'type': 'teleradiography',
                        'status': job.status
                    })
        except Exception as e:
            upload_results['messages'].append(f"Error creating teleradiography processing job: {e}")
        
        try:
            panoramic_file = request.FILES.get('panoramic')
            if panoramic_file:
                from ..file_utils import save_generic_modality_file
                fr, job = save_generic_modality_file(patient, 'panoramic', panoramic_file)
                if fr:
                    upload_results['messages'].append(f"Panoramic uploaded successfully")
                if job:
                    upload_results['jobs'].append({
                        'id': job.id,
                        'type': 'panoramic',
                        'status': job.status
                    })
        except Exception as e:
            upload_results['messages'].append(f"Error creating panoramic processing job: {e}")
        
        # Handle Rawzip
        try:
            rawzip_file = request.FILES.get('rawzip')
            if rawzip_file:
                from ..file_utils import save_generic_modality_file
                fr, job = save_generic_modality_file(patient, 'rawzip', rawzip_file)
        
                if fr:
                    upload_results['messages'].append(f"Rawzip uploaded successfully")
                if job:
                    upload_results['jobs'].append({
                        'id': job.id,
                        'type': 'rawzip',
                        'status': job.status
                    })
        except Exception as e:
            upload_results['messages'].append(f"Error creating rawzip processing job: {e}")
        
        # Prepare response data
        patient_data = {
            'patient_id': patient.patient_id,
            'name': patient.name,
            'project': {
                'id': project.id,
                'name': project.name,
                'slug': project.slug,
            },
            'folder': {
                'id': patient.folder.id,
                'name': patient.folder.name,
                'full_path': patient.folder.get_full_path(),
            } if patient.folder else None,
            'uploaded_at': patient.uploaded_at.isoformat(),
            'uploaded_by': {
                'id': patient.uploaded_by.id,
                'username': patient.uploaded_by.username,
            } if patient.uploaded_by else None,
            'modalities': [
                {
                    'id': modality.id,
                    'name': modality.name,
                    'slug': modality.slug,
                    'description': modality.description,
                    'label': modality.label,
                } for modality in patient.modalities.all()
            ],
            'tags': [tag.name for tag in patient.tags.all()],
            'processing_status': {
                'ios': patient.ios_job_status,
                'cbct': patient.cbct_job_status,
            },
            'upload_results': upload_results,
        }
        
        return JsonResponse({
            'success': True,
            'patient': patient_data,
            'message': 'Patient uploaded successfully!'
        })
        
    except Exception as e:
        logger.error(f"Error in project upload API for project {project_slug}: {e}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@require_http_methods(["GET"])
def get_project_folders(request, project_slug):
    """
    API endpoint to get all available folders for a project
    URL: /api/<project_slug>/folders/
    """
    try:
        Folder = _project_models(project_slug)['Folder']

        # Check if project exists
        try:
            project = Project.objects.get(slug=project_slug)
        except Project.DoesNotExist:
            return JsonResponse({'error': 'Project not found'}, status=404)

        if not request.user.is_authenticated:
            return JsonResponse({'error': 'Authentication required'}, status=401)
        
        # Get all folders - we'll get all folders as they can be used across projects
        folders = Folder.objects.filter(parent__isnull=True).order_by('name')
        folders = filter_folders_for_user(request.user, folders, _project_domain(project_slug))
        
        folders_data = []
        for folder in folders:
            folder_data = {
                'id': folder.id,
                'name': folder.name,
                'parent': {
                    'id': folder.parent.id,
                    'name': folder.parent.name,
                    'full_path': folder.parent.get_full_path(),
                } if folder.parent else None,
                'full_path': folder.get_full_path(),
                'created_at': folder.created_at.isoformat() if hasattr(folder, 'created_at') else None,
            }
            folders_data.append(folder_data)
        
        return JsonResponse({
            'success': True,
            'project': {
                'id': project.id,
                'name': project.name,
                'slug': project.slug,
            },
            'folders': folders_data,
            'total_folders': len(folders_data)
        })
        
    except Exception as e:
        logger.error(f"Error getting folders for project {project_slug}: {e}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
def project_patients_handler(request, project_slug):
    """
    Handler for /<project_slug>/api/patients/
    GET: Returns all patients for a project with their modalities
    POST: Returns files for multiple patients (bulk operation)
    """
    if request.method == 'GET':
        return get_project_patients_and_modalities(request, project_slug)
    elif request.method == 'POST':
        return get_multiple_patients_files(request, project_slug)
    else:
        return JsonResponse({'error': 'Method not allowed'}, status=405)


@csrf_exempt
@require_http_methods(["GET"])
def get_project_patients_and_modalities(request, project_slug):
    """
    API endpoint to get all patients for a project and their available modalities
    URL: /<project_slug>/api/patients/
    """
    try:
        Patient = _project_models(project_slug)['Patient']

        # Check if project exists
        try:
            project = Project.objects.get(slug=project_slug)
        except Project.DoesNotExist:
            return JsonResponse({'error': 'Project not found'}, status=404)

        if not request.user.is_authenticated:
            return JsonResponse({'error': 'Authentication required'}, status=401)
        
        # Get all patients for this project with their modalities
        patients = Patient.objects.all().prefetch_related('modalities').order_by('patient_id')
        patients = filter_patients_for_user(request.user, patients, _project_domain(project_slug))
        
        patients_data = []
        for patient in patients:
            patient_data = {
                'patient_id': patient.patient_id,
                'name': patient.name,
                'uploaded_at': patient.uploaded_at.isoformat(),
                'folder': {
                    'id': patient.folder.id,
                    'name': patient.folder.name,
                    'full_path': patient.folder.get_full_path(),
                } if patient.folder else None,
                'modalities': [
                    {
                        'id': modality.id,
                        'name': modality.name,
                        'slug': modality.slug,
                        'description': modality.description,
                        'icon': modality.icon,
                        'label': modality.label,
                        'supported_extensions': modality.supported_extensions,
                        'subtypes': modality.subtypes,
                        'requires_multiple_files': modality.requires_multiple_files,
                    } for modality in patient.modalities.all()
                ]
            }
            patients_data.append(patient_data)
        
        return JsonResponse({
            'success': True,
            'project': {
                'id': project.id,
                'name': project.name,
                'slug': project.slug,
                'description': project.description,
            },
            'patients': patients_data,
            'total_patients': len(patients_data)
        })
        
    except Exception as e:
        logger.error(f"Error getting patients and modalities for project {project_slug}: {e}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@require_http_methods(["GET"])
def get_patient_files(request, project_slug, patient_id):
    """
    API endpoint to get all files for a specific patient
    URL: /<project_slug>/api/patients/<patient_id>/files/
    """
    try:
        domain = _project_domain(project_slug)
        Patient = _project_models(project_slug)['Patient']

        # Check if project exists
        try:
            project = Project.objects.get(slug=project_slug)
        except Project.DoesNotExist:
            return JsonResponse({'error': 'Project not found'}, status=404)

        if not request.user.is_authenticated:
            return JsonResponse({'error': 'Authentication required'}, status=401)
        
        # Check if patient exists and belongs to the project
        try:
            patient = Patient.objects.get(patient_id=patient_id)
        except Patient.DoesNotExist:
            return JsonResponse({'error': 'Patient not found in this project'}, status=404)

        if not user_is_project_admin(request.user, domain):
            if not patient.folder or not user_can_read_folder(request.user, patient.folder, domain):
                return JsonResponse({'error': 'Permission denied'}, status=403)
        
        # Get all files for this patient from FileRegistry
        file_filter = {'domain': domain, 'patient': patient}
        files = FileRegistry.objects.filter(**file_filter).prefetch_related('modality').order_by('file_type', 'created_at')
        
        files_data = []
        for file_obj in files:
            file_data = {
                'id': file_obj.id,
                'filename': os.path.basename(file_obj.file_path),
                'file_type': file_obj.file_type,
                'file_type_display': file_obj.get_file_type_display(),
                'file_size': file_obj.file_size,
                'file_hash': file_obj.file_hash,
                'subtype': file_obj.subtype,
                'created_at': file_obj.created_at.isoformat(),
                'metadata': file_obj.metadata,
                'modality': {
                    'id': file_obj.modality.id,
                    'name': file_obj.modality.name,
                    'slug': file_obj.modality.slug,
                    'label': file_obj.modality.label,
                } if file_obj.modality else None
            }
            files_data.append(file_data)
        
        return JsonResponse({
            'success': True,
            'project': {
                'id': project.id,
                'name': project.name,
                'slug': project.slug,
            },
            'patient': {
                'patient_id': patient.patient_id,
                'name': patient.name,
                'uploaded_at': patient.uploaded_at.isoformat(),
            },
            'files': files_data,
            'total_files': len(files_data)
        })
        
    except Exception as e:
        logger.error(f"Error getting files for patient {patient_id} in project {project_slug}: {e}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        return JsonResponse({'error': str(e)}, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def get_multiple_patients_files(request, project_slug):
    """
    API endpoint to get files for multiple patients (bulk operation)
    URL: /<project_slug>/api/patients/
    
    Request body should contain:
    {
        "patient_ids": [1, 2, 3, ...]
    }
    """
    try:
        domain = _project_domain(project_slug)
        Patient = _project_models(project_slug)['Patient']

        # Check if project exists
        try:
            project = Project.objects.get(slug=project_slug)
        except Project.DoesNotExist:
            return JsonResponse({'error': 'Project not found'}, status=404)

        if not request.user.is_authenticated:
            return JsonResponse({'error': 'Authentication required'}, status=401)
        
        # Parse request data
        data = json.loads(request.body.decode('utf-8'))
        patient_ids = data.get('patient_ids', [])
        
        if not patient_ids:
            return JsonResponse({'error': 'patient_ids list is required'}, status=400)
        
        if not isinstance(patient_ids, list):
            return JsonResponse({'error': 'patient_ids must be a list'}, status=400)
            
        if len(patient_ids) > 100:  # Limit to prevent excessive load
            return JsonResponse({'error': 'Maximum 100 patient IDs allowed per request'}, status=400)
        
        # Get patients and their files (only from the specified project)
        files_prefetch = Prefetch(
            'files',
            queryset=FileRegistry.objects.filter(domain='maxillo').select_related('modality')
        )
        patients = Patient.objects.filter(patient_id__in=patient_ids).prefetch_related(files_prefetch).order_by('patient_id')
        patients = filter_patients_for_user(request.user, patients, domain)
        
        found_patient_ids = set(patients.values_list('patient_id', flat=True))
        missing_patient_ids = [pid for pid in patient_ids if pid not in found_patient_ids]
        
        patients_data = {}
        for patient in patients:
            # Get FileRegistry files
            files_data = []
            for file_obj in patient.files.all():
                file_data = {
                    'id': file_obj.id,
                    'filename': os.path.basename(file_obj.file_path),
                    'file_type': file_obj.file_type,
                    'file_type_display': file_obj.get_file_type_display(),
                    'file_size': file_obj.file_size,
                    'file_hash': file_obj.file_hash,
                    'subtype': file_obj.subtype,
                    'created_at': file_obj.created_at.isoformat(),
                    'metadata': file_obj.metadata,
                    'modality': {
                        'id': file_obj.modality.id,
                        'name': file_obj.modality.name,
                        'slug': file_obj.modality.slug,
                        'label': file_obj.modality.label,
                    } if file_obj.modality else None
                }
                files_data.append(file_data)
            
            patients_data[patient.patient_id] = {
                'patient_info': {
                    'patient_id': patient.patient_id,
                    'name': patient.name,
                    'uploaded_at': patient.uploaded_at.isoformat(),
                },
                'files': files_data,
                'total_files': len(files_data)
            }
        
        return JsonResponse({
            'success': True,
            'project': {
                'id': project.id,
                'name': project.name,
                'slug': project.slug,
            },
            'patients': patients_data,
            'found_patients': len(patients),
            'missing_patient_ids': missing_patient_ids,
            'total_requested': len(patient_ids)
        })
        
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON in request body'}, status=400)
    except Exception as e:
        logger.error(f"Error getting bulk patient files for project {project_slug}: {e}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        return JsonResponse({'error': str(e)}, status=500)
