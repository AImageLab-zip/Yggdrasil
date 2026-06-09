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
from common.permissions import user_can_read_folder, user_can_write_annotations, user_is_project_admin
from common.models import Job

from ..models import IntraoralToothSegmentation
from .domain import get_domain_models
from .patient_data import _latest_official_image_file

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
    if user_is_project_admin(user_profile.user, patient._meta.app_label):
        return True
    return bool(patient.folder and user_can_read_folder(user_profile.user, patient.folder, patient._meta.app_label))


def _can_modify(user_profile, patient):
    if user_is_project_admin(user_profile.user, patient._meta.app_label):
        return True
    return bool(patient.folder and user_can_write_annotations(user_profile.user, patient.folder, patient._meta.app_label))


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


def _clone_teeth(teeth):
    if not isinstance(teeth, dict):
        return {}
    cloned = {}
    for tooth_code, polygons in teeth.items():
        if not isinstance(polygons, list):
            continue
        cloned[str(tooth_code)] = [
            [[round(float(point[0]), 3), round(float(point[1]), 3)] for point in polygon]
            for polygon in polygons
            if isinstance(polygon, list)
        ]
    return cloned


def _clip_polygon_to_rect(polygon, left, top, right, bottom):
    def inside(point, edge):
        x, y = point
        if edge == 'left':
            return x >= left
        if edge == 'right':
            return x <= right
        if edge == 'top':
            return y >= top
        return y <= bottom

    def intersect(start, end, edge):
        x1, y1 = start
        x2, y2 = end
        if edge in ('left', 'right'):
            x = left if edge == 'left' else right
            dx = x2 - x1
            if abs(dx) < 1e-9:
                return [x, y1]
            t = (x - x1) / dx
            return [x, y1 + (y2 - y1) * t]
        y = top if edge == 'top' else bottom
        dy = y2 - y1
        if abs(dy) < 1e-9:
            return [x1, y]
        t = (y - y1) / dy
        return [x1 + (x2 - x1) * t, y]

    def clip_against(points, edge):
        output = []
        if not points:
            return output
        for idx, current in enumerate(points):
            previous = points[idx - 1]
            current_inside = inside(current, edge)
            previous_inside = inside(previous, edge)
            if current_inside:
                if not previous_inside:
                    output.append(intersect(previous, current, edge))
                output.append(current)
            elif previous_inside:
                output.append(intersect(previous, current, edge))
        return output

    result = [list(point) for point in polygon]
    for edge in ('left', 'right', 'top', 'bottom'):
        result = clip_against(result, edge)
        if len(result) < 3:
            return []
    return [[round(point[0], 3), round(point[1], 3)] for point in result]


def _apply_edit_operation(point, operation):
    x = float(point[0])
    y = float(point[1])
    op_type = operation.get('type')
    if op_type == 'flip-h':
        width = float(operation.get('input_width') or 0)
        return [round(width - x, 3), round(y, 3)]
    if op_type == 'flip-v':
        height = float(operation.get('input_height') or 0)
        return [round(x, 3), round(height - y, 3)]
    if op_type == 'crop':
        return [round(x - float(operation.get('x') or 0), 3), round(y - float(operation.get('y') or 0), 3)]
    return [round(x, 3), round(y, 3)]


def _transform_polygon(polygon, operations):
    transformed = [list(point) for point in polygon]
    for operation in operations or []:
        if len(transformed) < 3:
            return []
        op_type = operation.get('type')
        if op_type == 'crop':
            left = float(operation.get('x') or 0)
            top = float(operation.get('y') or 0)
            right = left + float(operation.get('width') or 0)
            bottom = top + float(operation.get('height') or 0)
            transformed = _clip_polygon_to_rect(transformed, left, top, right, bottom)
            if len(transformed) < 3:
                return []
        transformed = [_apply_edit_operation(point, operation) for point in transformed]
    return transformed if len(transformed) >= 3 else []


def _transform_teeth(teeth, edit_meta):
    operations = edit_meta.get('operations') if isinstance(edit_meta, dict) else []
    if not operations:
        return _clone_teeth(teeth)

    transformed_teeth = {}
    for tooth_code, polygons in (teeth or {}).items():
        next_polygons = []
        for polygon in polygons or []:
            transformed_polygon = _transform_polygon(polygon, operations)
            if len(transformed_polygon) >= 3:
                next_polygons.append(transformed_polygon)
        if next_polygons:
            transformed_teeth[str(tooth_code)] = next_polygons
    return transformed_teeth


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
        raw_files = patient.files.filter(file_type='intraoral_raw').order_by(
            'metadata__image_index', 'created_at', 'id'
        )
        images = []
        file_ids = set()
        fallback_index = 1
        if raw_files.exists():
            for file_obj in raw_files:
                if not file_obj.file_path or not artifact_exists(file_obj.file_path):
                    continue

                image_index = fallback_index
                if isinstance(file_obj.metadata, dict):
                    image_index = file_obj.metadata.get('image_index') or fallback_index

                official_file = _latest_official_image_file(
                    patient,
                    ['intraoral-photo_processed', 'intraoral_processed'],
                    source_file_id=file_obj.id,
                )
                if not official_file:
                    official_file = _latest_official_image_file(
                        patient,
                        ['intraoral-photo_processed', 'intraoral_processed'],
                        image_index=image_index,
                    )
                official_file = official_file or file_obj
                images.append({
                    'id': official_file.id,
                    'source_file_id': file_obj.id,
                    'index': image_index,
                    'original_filename': (
                        file_obj.metadata.get('original_filename', '')
                        if isinstance(file_obj.metadata, dict)
                        else ''
                    ),
                    'url': _serve_file_url(request, official_file.id),
                    'raw_url': _serve_file_url(request, file_obj.id),
                    'edit_meta': (
                        official_file.metadata.get('edit_meta')
                        if isinstance(official_file.metadata, dict)
                        else None
                    ),
                })
                file_ids.add(official_file.id)
                file_ids.add(file_obj.id)
                fallback_index += 1
        else:
            legacy_files = patient.files.filter(
                file_type__in=['intraoral-photo_processed', 'intraoral_processed']
            ).order_by('metadata__image_index', 'created_at', 'id')
            for file_obj in legacy_files:
                if not file_obj.file_path or not artifact_exists(file_obj.file_path):
                    continue
                image_index = fallback_index
                if isinstance(file_obj.metadata, dict):
                    image_index = file_obj.metadata.get('image_index') or fallback_index
                source_file_id = (
                    file_obj.metadata.get('source_file_id', file_obj.id)
                    if isinstance(file_obj.metadata, dict)
                    else file_obj.id
                )
                raw_url = _serve_file_url(request, source_file_id) if source_file_id else _serve_file_url(request, file_obj.id)
                images.append({
                    'id': file_obj.id,
                    'source_file_id': source_file_id,
                    'index': image_index,
                    'original_filename': (
                        file_obj.metadata.get('original_filename', '')
                        if isinstance(file_obj.metadata, dict)
                        else ''
                    ),
                    'url': _serve_file_url(request, file_obj.id),
                    'raw_url': raw_url,
                    'edit_meta': (
                        file_obj.metadata.get('edit_meta')
                        if isinstance(file_obj.metadata, dict)
                        else None
                    ),
                })
                file_ids.add(file_obj.id)
                if source_file_id:
                    file_ids.add(source_file_id)
                fallback_index += 1

        segmentation_qs = IntraoralToothSegmentation.objects.filter(
            patient=patient,
            image_file_id__in=list(file_ids),
        )
        by_file_id = {row.image_file_id: row for row in segmentation_qs}

        for image in images:
            row = by_file_id.get(image['id'])
            source_row = by_file_id.get(image.get('source_file_id')) if image.get('source_file_id') != image['id'] else None
            display_row = row or source_row
            if row:
                teeth = row.teeth or {}
            elif source_row:
                teeth = _transform_teeth(source_row.teeth or {}, image.get('edit_meta') or {})
            else:
                teeth = {}
            raw_teeth = source_row.teeth if source_row else (row.teeth if row else {})

            image['teeth'] = teeth
            image['raw_teeth'] = raw_teeth or {}
            image['is_confirmed'] = bool(display_row and display_row.is_confirmed)
            image['confirmed_at'] = (
                display_row.confirmed_at.isoformat() if display_row and display_row.confirmed_at else None
            )
            image['confirmed_by'] = (
                display_row.confirmed_by.username if display_row and display_row.confirmed_by else None
            )
            image['updated_at'] = (
                display_row.updated_at.isoformat() if display_row and display_row.updated_at else None
            )
            image['updated_by'] = (
                display_row.updated_by.username if display_row and display_row.updated_by else None
            )

        running_job_statuses = ['pending', 'dependency', 'processing', 'retrying']
        segmentation_job_running = bool(
            Job.objects.filter(
                modality_slug='intraoral-photo',
                patient=patient,
                status__in=running_job_statuses,
            ).exists()
        )

        return JsonResponse({
            'images': images,
            'count': len(images),
            'tooth_codes': TOOTH_CODES,
            'can_modify': _can_modify(user_profile, patient),
            'segmentation_job_running': segmentation_job_running,
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
        file_type__in=['intraoral_raw', 'intraoral-photo_processed', 'intraoral_processed'],
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
