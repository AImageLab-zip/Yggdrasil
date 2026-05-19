"""Patient deletion views."""
from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST
import json
import logging

from .domain import get_domain_models
from common.permissions import (
    user_can_delete_single_patient,
    user_can_perform_bulk_operations,
    user_is_project_admin,
)

logger = logging.getLogger(__name__)

@login_required
@require_POST
def delete_patient(request, patient_id):
    """Soft delete a patient by marking it as deleted."""
    Patient = get_domain_models(request)['Patient']

    try:
        patient = get_object_or_404(Patient, patient_id=patient_id)
        can_delete = bool(patient.folder and user_can_delete_single_patient(request.user, patient.folder, request))
        if user_is_project_admin(request.user, request):
            can_delete = True

        if not can_delete:
            return JsonResponse({
                'success': False,
                'error': 'You do not have permission to delete this patient.'
            }, status=403)

        patient.deleted = True
        patient.save(update_fields=['deleted'])

        return JsonResponse({'success': True, 'message': 'Scan deleted successfully'})

    except Exception as e:
        logger.error(f"Error deleting scan {patient_id}: {e}", exc_info=True)
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
@require_POST
def bulk_delete_patients(request):
    """Bulk soft delete scans by marking them as deleted."""
    Patient = get_domain_models(request)['Patient']

    try:
        data = json.loads(request.body) if request.body else request.POST
        scan_ids = data.get('scan_ids', [])

        if not isinstance(scan_ids, list) or not scan_ids:
            return JsonResponse({'error': 'scan_ids list is required'}, status=400)

        if not user_can_perform_bulk_operations(request.user, request):
            return JsonResponse({
                'success': False,
                'error': 'You do not have permission to bulk delete scans.'
            }, status=403)

        scans_to_delete = Patient.objects.filter(patient_id__in=scan_ids)

        if not scans_to_delete.exists():
            return JsonResponse({'error': 'No valid scans found to delete'}, status=404)

        deleted_count = scans_to_delete.update(deleted=True)

        return JsonResponse({
            'success': True,
            'message': f'Successfully deleted {deleted_count} scans.',
            'deleted_count': deleted_count,
        })

    except Exception as e:
        logger.error(f"Error in bulk delete: {e}", exc_info=True)
        return JsonResponse({'success': False, 'error': str(e)}, status=500)
