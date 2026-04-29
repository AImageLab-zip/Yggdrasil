"""Intraoral tooth segmentation API endpoints."""

import json
import logging
import math

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.urls import reverse
from django.views.decorators.http import require_POST
from PIL import Image

from common.file_access import exists as artifact_exists, open_binary

from ..models import IntraoralToothSegmentation
from .domain import get_domain_models

logger = logging.getLogger(__name__)


TOOTH_CODES = [
    '18', '17', '16', '15', '14', '13', '12', '11',
    '21', '22', '23', '24', '25', '26', '27', '28',
    '48', '47', '46', '45', '44', '43', '42', '41',
    '31', '32', '33', '34', '35', '36', '37', '38',
]
TOOTH_CODE_SET = set(TOOTH_CODES)
MAX_POINTS_PER_TOOTH = 500
MISSING = object()


def _serve_file_url(request, file_id):
    namespace = (
        getattr(request, 'resolver_match', None) and request.resolver_match.namespace
    ) or 'maxillo'
    return reverse(f'{namespace}:api_serve_file', kwargs={'file_id': file_id})


def _can_view(user_profile, patient):
    if user_profile.is_admin():
        return True
    if user_profile.is_annotator() and patient.visibility != 'debug':
        return True
    if user_profile.is_student_developer() and patient.visibility == 'debug':
        return True
    if patient.visibility == 'public':
        return True
    return False


def _can_modify(user_profile, patient):
    if user_profile.is_admin():
        return True
    if user_profile.is_annotator() and patient.visibility != 'debug':
        return True
    if user_profile.is_student_developer() and patient.visibility == 'debug':
        return True
    return False


def _image_bounds_from_metadata(metadata):
    if not isinstance(metadata, dict):
        return None

    width = metadata.get('image_width') or metadata.get('width')
    height = metadata.get('image_height') or metadata.get('height')
    try:
        width = float(width)
        height = float(height)
    except (TypeError, ValueError):
        return None

    if width > 0 and height > 0:
        return width, height
    return None


def _get_image_bounds(file_obj):
    bounds = _image_bounds_from_metadata(file_obj.metadata)
    if bounds:
        return bounds

    if not file_obj.file_path:
        raise ValueError('Unable to determine image dimensions.')

    try:
        body, _ = open_binary(file_obj.file_path)
        try:
            with Image.open(body) as img:
                width, height = img.size
        finally:
            close = getattr(body, 'close', None)
            if close:
                close()
    except Exception as exc:
        logger.warning(
            'Unable to inspect intraoral image dimensions for file %s: %s',
            file_obj.id,
            exc,
        )
        raise ValueError('Unable to determine image dimensions.')

    metadata = dict(file_obj.metadata or {})
    metadata['image_width'] = width
    metadata['image_height'] = height
    file_obj.metadata = metadata
    file_obj.save(update_fields=['metadata'])
    return float(width), float(height)


def _normalize_polygon(points, image_bounds=None):
    if not isinstance(points, list):
        raise ValueError('Polygon must be a list of points.')

    if len(points) < 3:
        raise ValueError('Polygon must have at least 3 points.')
    if len(points) > MAX_POINTS_PER_TOOTH:
        raise ValueError(f'Polygon exceeds {MAX_POINTS_PER_TOOTH} points.')

    normalized = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise ValueError('Each point must be [x, y].')

        x = float(point[0])
        y = float(point[1])
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError('Point coordinates must be finite numbers.')
        if image_bounds:
            width, height = image_bounds
            if x < 0 or y < 0 or x > width or y > height:
                raise ValueError('Point coordinates must stay inside image bounds.')

        normalized.append([round(x, 3), round(y, 3)])

    return normalized


def _is_point(value):
    return (
        isinstance(value, list)
        and len(value) >= 2
        and not isinstance(value[0], (list, tuple))
        and not isinstance(value[1], (list, tuple))
    )


def _normalize_polygons(value, image_bounds=None):
    if not isinstance(value, list):
        raise ValueError('Polygon set must be a list.')
    if not value:
        return []
    if _is_point(value[0]):
        return [_normalize_polygon(value, image_bounds)]
    return [_normalize_polygon(polygon, image_bounds) for polygon in value if polygon]


def _normalize_teeth_payload(teeth_payload, image_bounds=None):
    if teeth_payload in (None, ''):
        return {}
    if not isinstance(teeth_payload, dict):
        raise ValueError('Teeth payload must be an object.')

    normalized = {}
    for tooth_code, polygon in teeth_payload.items():
        code = str(tooth_code).strip()
        if code not in TOOTH_CODE_SET:
            raise ValueError(f'Unsupported tooth code: {code}')
        polygons = _normalize_polygons(polygon, image_bounds)
        if polygons:
            normalized[code] = polygons

    return normalized


@login_required
def patient_intraoral_segmentation_data(request, patient_id):
    """Return intraoral images and stored tooth polygons for one patient."""
    Patient = get_domain_models(request)['Patient']
    patient = get_object_or_404(Patient, patient_id=patient_id)
    user_profile = request.user.profile

    if not _can_view(user_profile, patient):
        return JsonResponse({'error': 'Permission denied'}, status=403)

    try:
        files_qs = patient.files.filter(
            file_type__in=['intraoral_raw', 'intraoral_processed']
        ).order_by('metadata__image_index', 'created_at', 'id')

        images = []
        file_ids = []
        fallback_index = 1
        for file_obj in files_qs:
            if not file_obj.file_path or not artifact_exists(file_obj.file_path):
                continue

            image_index = fallback_index
            if isinstance(file_obj.metadata, dict):
                image_index = file_obj.metadata.get('image_index') or fallback_index

            images.append({
                'id': file_obj.id,
                'index': image_index,
                'original_filename': (
                    file_obj.metadata.get('original_filename', '')
                    if isinstance(file_obj.metadata, dict)
                    else ''
                ),
                'url': _serve_file_url(request, file_obj.id),
            })
            file_ids.append(file_obj.id)
            fallback_index += 1

        segmentation_qs = IntraoralToothSegmentation.objects.filter(
            patient=patient,
            image_file_id__in=file_ids,
        )
        by_file_id = {row.image_file_id: row for row in segmentation_qs}

        for image in images:
            row = by_file_id.get(image['id'])
            image['teeth'] = row.teeth if row else {}
            image['is_confirmed'] = bool(row and row.is_confirmed)
            image['confirmed_at'] = (
                row.confirmed_at.isoformat() if row and row.confirmed_at else None
            )
            image['confirmed_by'] = (
                row.confirmed_by.username if row and row.confirmed_by else None
            )
            image['updated_at'] = (
                row.updated_at.isoformat() if row and row.updated_at else None
            )
            image['updated_by'] = (
                row.updated_by.username if row and row.updated_by else None
            )

        return JsonResponse({
            'images': images,
            'count': len(images),
            'tooth_codes': TOOTH_CODES,
            'can_modify': _can_modify(user_profile, patient),
        })
    except Exception as exc:
        logger.error(
            'Error loading intraoral segmentation for patient %s: %s',
            patient_id,
            exc,
            exc_info=True,
        )
        return JsonResponse({'error': 'Internal server error'}, status=500)


@login_required
@require_POST
def update_patient_intraoral_segmentation(request, patient_id):
    """Create or update intraoral tooth polygons for one patient."""
    Patient = get_domain_models(request)['Patient']
    patient = get_object_or_404(Patient, patient_id=patient_id)
    user_profile = request.user.profile

    if not _can_modify(user_profile, patient):
        return JsonResponse({'error': 'Permission denied'}, status=403)

    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON payload'}, status=400)

    images_payload = payload.get('images', [])
    if not isinstance(images_payload, list):
        return JsonResponse({'error': '"images" must be a list.'}, status=400)

    file_ids = []
    for item in images_payload:
        if not isinstance(item, dict):
            return JsonResponse({'error': 'Each image item must be an object.'}, status=400)
        file_id = item.get('file_id')
        if not isinstance(file_id, int):
            return JsonResponse({'error': 'Each image item needs integer "file_id".'}, status=400)
        file_ids.append(file_id)

    if len(file_ids) != len(set(file_ids)):
        return JsonResponse({'error': 'Duplicate image file ids are not allowed.'}, status=400)

    valid_files_qs = patient.files.filter(
        id__in=file_ids,
        file_type__in=['intraoral_raw', 'intraoral_processed'],
    )
    valid_files_by_id = {file_obj.id: file_obj for file_obj in valid_files_qs}
    valid_file_ids = set(valid_files_by_id)

    invalid_file_ids = [file_id for file_id in file_ids if file_id not in valid_file_ids]
    if invalid_file_ids:
        return JsonResponse({'error': f'Invalid intraoral file ids: {invalid_file_ids}'}, status=400)

    updated_count = 0
    deleted_count = 0
    updated_images = []
    try:
        with transaction.atomic():
            existing_rows = {
                row.image_file_id: row
                for row in IntraoralToothSegmentation.objects.select_for_update().filter(
                    patient=patient,
                    image_file_id__in=file_ids,
                )
            }

            normalized_items = []
            for item in images_payload:
                file_id = item['file_id']
                existing_row = existing_rows.get(file_id)
                expected_updated_at = item.get('updated_at', MISSING)
                if expected_updated_at is not MISSING:
                    current_updated_at = (
                        existing_row.updated_at.isoformat()
                        if existing_row and existing_row.updated_at
                        else None
                    )
                    if current_updated_at != expected_updated_at:
                        return JsonResponse({
                            'error': 'Segmentation changed elsewhere. Reload before editing.',
                            'file_id': file_id,
                            'updated_at': current_updated_at,
                        }, status=409)

                requested_confirmation = item.get('is_confirmed', MISSING)
                if requested_confirmation is not MISSING and not isinstance(requested_confirmation, bool):
                    return JsonResponse({'error': '"is_confirmed" must be true or false.'}, status=400)

                teeth_payload = item.get('teeth', {})
                image_bounds = None
                if teeth_payload not in (None, '', {}):
                    image_bounds = _get_image_bounds(valid_files_by_id[file_id])
                normalized_teeth = _normalize_teeth_payload(teeth_payload, image_bounds)
                if (
                    existing_row
                    and existing_row.is_confirmed
                    and requested_confirmation is not False
                    and normalized_teeth != (existing_row.teeth or {})
                ):
                    return JsonResponse({
                        'error': 'Segmentation is confirmed. Reopen before editing.',
                        'file_id': file_id,
                    }, status=409)

                normalized_items.append((file_id, normalized_teeth, requested_confirmation))

            for file_id, normalized_teeth, requested_confirmation in normalized_items:
                existing_row = existing_rows.get(file_id)
                is_confirmed = (
                    requested_confirmation
                    if requested_confirmation is not MISSING
                    else bool(existing_row and existing_row.is_confirmed)
                )
                confirmation_defaults = {}
                if requested_confirmation is True:
                    confirmation_defaults = {
                        'is_confirmed': True,
                        'confirmed_by': request.user,
                        'confirmed_at': timezone.now(),
                    }
                elif requested_confirmation is False:
                    confirmation_defaults = {
                        'is_confirmed': False,
                        'confirmed_by': None,
                        'confirmed_at': None,
                    }

                if normalized_teeth or is_confirmed:
                    row, _ = IntraoralToothSegmentation.objects.update_or_create(
                        patient=patient,
                        image_file_id=file_id,
                        defaults={
                            'teeth': normalized_teeth,
                            'updated_by': request.user,
                            **confirmation_defaults,
                        },
                    )
                    updated_count += 1
                    updated_images.append({
                        'file_id': file_id,
                        'updated_at': row.updated_at.isoformat() if row.updated_at else None,
                        'is_confirmed': row.is_confirmed,
                        'confirmed_at': row.confirmed_at.isoformat() if row.confirmed_at else None,
                        'confirmed_by': row.confirmed_by.username if row.confirmed_by else None,
                    })
                else:
                    deleted_count += IntraoralToothSegmentation.objects.filter(
                        patient=patient,
                        image_file_id=file_id,
                    ).delete()[0]
                    updated_images.append({
                        'file_id': file_id,
                        'updated_at': None,
                        'is_confirmed': False,
                        'confirmed_at': None,
                        'confirmed_by': None,
                    })

        return JsonResponse({
            'success': True,
            'updated_count': updated_count,
            'deleted_count': deleted_count,
            'images': updated_images,
        })
    except ValueError as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    except Exception as exc:
        logger.error(
            'Error saving intraoral segmentation for patient %s: %s',
            patient_id,
            exc,
            exc_info=True,
        )
        return JsonResponse({'error': 'Internal server error'}, status=500)
