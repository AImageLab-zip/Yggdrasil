"""Classification update views."""
import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .domain import get_domain_models
from common.permissions import user_can_write_annotations, user_is_project_admin


@login_required
@require_POST
@csrf_exempt
def update_classification(request, patient_id):
    """AJAX endpoint for instant classification updates"""
    domain_models = get_domain_models(request)
    Patient = domain_models['Patient']
    Classification = domain_models['Classification']
    
    try:
        patient = get_object_or_404(Patient, patient_id=patient_id)
        
        can_classify = bool(patient.folder and user_can_write_annotations(request.user, patient.folder, request))
        if user_is_project_admin(request.user, request):
            can_classify = True
        
        if not can_classify:
            return JsonResponse({'error': 'Permission denied'}, status=403)
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON payload'}, status=400)
        
        field = data.get('field')
        value = data.get('value')
        
        valid_fields = ['sagittal_left', 'sagittal_right', 'vertical', 'transverse', 'midline']
        if field not in valid_fields:
            return JsonResponse({'error': 'Invalid field'}, status=400)

        valid_values = {choice[0] for choice in Classification._meta.get_field(field).flatchoices}
        if value not in valid_values:
            return JsonResponse({'error': 'Invalid value'}, status=400)

        defaults = {
            'sagittal_left': 'Unknown',
            'sagittal_right': 'Unknown',
            'vertical': 'Unknown',
            'transverse': 'Unknown',
            'midline': 'Unknown',
            'annotator': request.user,
        }
        ai_classification = patient.classifications.filter(classifier='pipeline').first()
        if ai_classification:
            for classification_field in valid_fields:
                defaults[classification_field] = getattr(ai_classification, classification_field)
        manual_classification, created = Classification.objects.get_or_create(
            patient=patient,
            classifier='manual',
            defaults=defaults,
        )
        
        setattr(manual_classification, field, value)
        manual_classification.save()
        
        return JsonResponse({
            'success': True,
            'field': field,
            'value': value,
            'display_value': getattr(manual_classification, f'get_{field}_display')()
        })
        
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
