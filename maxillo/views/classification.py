"""Classification update views."""
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
import json
import os
import logging

from .domain import get_domain_models
from common.permissions import user_can_write_annotations, user_is_project_admin

logger = logging.getLogger(__name__)


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
        data = json.loads(request.body)
        
        field = data.get('field')
        value = data.get('value')
        
        valid_fields = ['sagittal_left', 'sagittal_right', 'vertical', 'transverse', 'midline']
        if field not in valid_fields:
            return JsonResponse({'error': 'Invalid field'}, status=400)
        
        manual_classification, created = Classification.objects.get_or_create(
            patient=patient,
            classifier='manual',
            defaults={
                'sagittal_left': 'Unknown',
                'sagittal_right': 'Unknown',
                'vertical': 'Unknown',
                'transverse': 'Unknown',
                'midline': 'Unknown',
                'annotator': request.user,
            }
        )
        
        if created:
            ai_classification = patient.classifications.filter(classifier='pipeline').first()
            if ai_classification:
                manual_classification.sagittal_left = ai_classification.sagittal_left
                manual_classification.sagittal_right = ai_classification.sagittal_right
                manual_classification.vertical = ai_classification.vertical
                manual_classification.transverse = ai_classification.transverse
                manual_classification.midline = ai_classification.midline
        
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
