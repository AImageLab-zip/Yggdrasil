"""NIFTI metadata management views."""

import copy
import hashlib
import json
import logging
import os
import shutil
import tempfile

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from common.annotation_lock import annotation_lock_reasons, lock_message
from common.models import FileRegistry, Job
from common.object_storage import download_to_tempfile, get_object_storage
from common.permissions import user_can_edit_metadata, user_can_read_folder, user_is_project_admin

from ..models import PanoramicState
from .domain import get_domain_models
from .patient_detail import _resolved_cbct_viewer_source

logger = logging.getLogger(__name__)


def _artifact_path(file_obj, file_key):
    if not file_obj:
        return None
    file_key = file_key or "primary"
    if file_key == "primary":
        path = file_obj.file_path
    else:
        metadata = file_obj.metadata if isinstance(file_obj.metadata, dict) else {}
        files = metadata.get("files", {})
        output = files.get(file_key, {}) if isinstance(files, dict) else {}
        path = output.get("path") if isinstance(output, dict) else None

    if not isinstance(path, str) or not path.endswith((".nii", ".nii.gz")):
        return None
    return path


def _active_cbct_path(patient):
    """Return the exact display volume object key selected by the CBCT viewer."""
    source = _resolved_cbct_viewer_source(patient)
    if not source:
        return None
    return _artifact_path(source.get("file"), source.get("file_key"))


def _metadata_payload(request, nifti_img):
    """Build the complete JSON-safe metadata response for a loaded NIfTI image."""
    header = nifti_img.header
    try:
        affine = nifti_img.affine.tolist()
        if len(affine) != 4 or any(len(row) != 4 for row in affine):
            raise ValueError("Invalid affine matrix structure")
    except Exception as exc:
        logger.error("Error processing affine matrix: %s", exc)
        affine = [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ]

    try:
        voxel_dims = header.get_zooms()[:3]
        if len(voxel_dims) < 3:
            voxel_dims = [1.0, 1.0, 1.0]
    except Exception:
        voxel_dims = [1.0, 1.0, 1.0]

    try:
        shape = nifti_img.shape[:3]
        if len(shape) < 3:
            shape = [1, 1, 1]
    except Exception:
        shape = [1, 1, 1]

    try:
        from nibabel.orientations import aff2axcodes

        orientation_codes = aff2axcodes(nifti_img.affine)
        orientation = "".join(orientation_codes) if orientation_codes else "unknown"
    except Exception:
        orientation = "unknown"

    try:
        xyzt_units = header.get_xyzt_units()
        spatial_unit = str(xyzt_units[0]) if xyzt_units and xyzt_units[0] else "unknown"
        temporal_unit = (
            str(xyzt_units[1])
            if xyzt_units and len(xyzt_units) > 1 and xyzt_units[1]
            else "unknown"
        )
    except Exception:
        spatial_unit = "unknown"
        temporal_unit = "unknown"

    try:
        description = str(header.get("descrip", "")) if header.get("descrip") else ""
    except Exception:
        description = ""

    return {
        "affine": affine,
        "orientation": str(orientation),
        "voxel_dimensions": [float(value) for value in voxel_dims],
        "shape": [int(value) for value in shape],
        "data_type": str(header.get_data_dtype()),
        "units": {"spatial": spatial_unit, "temporal": temporal_unit},
        "description": description,
        "can_edit": bool(user_is_project_admin(request.user, request)),
    }


def _file_identity(path):
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _save_with_affine(nifti_img, affine, output_path):
    """Rewrite spatial transforms while retaining the image proxy and data dtype."""
    import nibabel as nib

    qform_code = int(nifti_img.header["qform_code"])
    sform_code = int(nifti_img.header["sform_code"])
    nifti_img.set_qform(affine, code=qform_code or 1)
    nifti_img.set_sform(affine, code=sform_code or 1)
    # Output differs from the proxy input, so nibabel can stream the original
    # dtype instead of materializing a float64 array with get_fdata().
    nib.save(nifti_img, output_path)


def _update_file_identities(prepared):
    updates_by_file = {}
    for artifact in prepared:
        updates_by_file.setdefault(artifact["file"].pk, []).append(artifact)

    for file_id, updates in updates_by_file.items():
        file_obj = FileRegistry.objects.select_for_update().get(pk=file_id)
        metadata = copy.deepcopy(file_obj.metadata) if isinstance(file_obj.metadata, dict) else {}
        update_fields = set()
        for artifact in updates:
            if artifact["file_key"] == "primary":
                file_obj.file_size = artifact["size"]
                file_obj.file_hash = artifact["sha256"]
                update_fields.update(("file_size", "file_hash"))
                continue

            files = metadata.setdefault("files", {})
            nested = files.setdefault(artifact["file_key"], {})
            nested["sha256"] = artifact["sha256"]
            nested["file_hash"] = artifact["sha256"]
            nested["file_size"] = artifact["size"]
            update_fields.add("metadata")

        if "metadata" in update_fields:
            file_obj.metadata = metadata
        if update_fields:
            file_obj.save(update_fields=sorted(update_fields))


def _restore_uploaded_objects(storage, attempted):
    failures = []
    for artifact in reversed(attempted):
        try:
            storage.upload_file(
                artifact["original_path"],
                key=artifact["path"],
                content_type="application/octet-stream",
            )
        except Exception as exc:
            failures.append(f"{artifact['path']}: {exc}")
    return failures


def _get_nifti_metadata(request, patient_id):
    """Get NIFTI metadata including origin, affine matrix, and orientation"""
    try:
        Patient = get_domain_models(request)["Patient"]
        patient = get_object_or_404(Patient, patient_id=patient_id)
        if not (user_is_project_admin(request.user, request) or (patient.folder and user_can_read_folder(request.user, patient.folder, request))):
            return JsonResponse({"error": "Permission denied"}, status=403)

        # Check if CBCT exists
        if not patient.has_cbct_scan():
            return JsonResponse({"error": "No CBCT scan available"}, status=404)

        cbct_path = _active_cbct_path(patient)
        if not cbct_path:
            return JsonResponse({"error": "CBCT file not found"}, status=404)

        import nibabel as nib
        try:
            suffix = ".nii.gz" if cbct_path.endswith(".nii.gz") else ".nii"
            with download_to_tempfile(cbct_path, suffix=suffix) as tmp_path:
                nifti_img = nib.load(tmp_path)
                return JsonResponse(_metadata_payload(request, nifti_img))

        except Exception as e:
            logger.error(f"Error loading NIFTI metadata: {e}")
            return JsonResponse(
                {"error": f"Error loading NIFTI file: {str(e)}"}, status=500
            )

    except Exception as e:
        logger.error(f"Error getting NIFTI metadata: {e}")
        return JsonResponse({"error": str(e)}, status=500)


@login_required
@require_GET
def get_nifti_metadata(request, patient_id):
    return _get_nifti_metadata(request, patient_id)


@login_required
@require_POST
def update_nifti_metadata(request, patient_id):
    """Update NIFTI metadata (admin only)"""
    try:
        Patient = get_domain_models(request)["Patient"]
        patient = get_object_or_404(Patient, patient_id=patient_id)

        if not user_can_edit_metadata(request.user, patient):
            return JsonResponse({"error": "Permission denied"}, status=403)

        # Rewriting qform/sform re-bases every landmark, spline and polygon ever
        # drawn on this volume: the voxels keep their values but move in patient
        # space, and nothing in the record would say so. Same rule as adding or
        # removing a raw file -- once annotation work exists, the raw inputs are
        # frozen, and unlike the Django admin nothing in the app can override it.
        lock_reasons = annotation_lock_reasons(patient)
        if lock_reasons:
            return JsonResponse(
                {
                    "error": lock_message(lock_reasons, subject="scan orientation"),
                    "raw_locked": True,
                },
                status=409,
            )

        # Check if CBCT exists
        if not patient.has_cbct_scan():
            return JsonResponse({"error": "No CBCT scan available"}, status=404)

        source = _resolved_cbct_viewer_source(patient)
        if not source:
            return JsonResponse({"error": "CBCT file not found"}, status=404)

        volume = {
            "role": "volume",
            "file": source.get("file"),
            "file_key": source.get("file_key") or "primary",
        }
        volume["path"] = _artifact_path(volume["file"], volume["file_key"])
        if not volume["path"]:
            return JsonResponse({"error": "CBCT file not found"}, status=404)

        artifacts = [volume]
        segmentation_file = source.get("segmentation_file")
        if segmentation_file:
            segmentation = {
                "role": "segmentation",
                "file": segmentation_file,
                "file_key": source.get("segmentation_key") or "primary",
            }
            segmentation["path"] = _artifact_path(
                segmentation["file"], segmentation["file_key"]
            )
            if not segmentation["path"]:
                return JsonResponse(
                    {"error": "Paired CBCT segmentation file not found"}, status=409
                )
            artifacts.append(segmentation)

        # Parse request data
        try:
            data = json.loads(request.body)
            if not isinstance(data, dict):
                raise ValueError("Request body must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            return JsonResponse({"error": f"Invalid request: {exc}"}, status=400)
        new_origin = data.get("origin")
        new_affine = data.get("affine")

        if new_origin is None and new_affine is None:
            return JsonResponse({"error": "No metadata to update"}, status=400)

        import nibabel as nib
        import numpy as np

        try:
            with tempfile.TemporaryDirectory() as work_dir:
                prepared = []
                target_affine = None
                for index, artifact in enumerate(artifacts):
                    suffix = ".nii.gz" if artifact["path"].endswith(".nii.gz") else ".nii"
                    with download_to_tempfile(artifact["path"], suffix=suffix) as local_path:
                        nifti_img = nib.load(local_path)
                        if artifact["role"] == "volume":
                            target_affine = nifti_img.affine.copy()
                            if new_affine is not None:
                                target_affine = np.array(new_affine, dtype=np.float64)
                                if target_affine.shape != (4, 4):
                                    raise ValueError("Affine matrix must be 4x4")
                            else:
                                origin = np.array(new_origin, dtype=np.float64)
                                if origin.shape != (3,):
                                    raise ValueError("Origin must have 3 coordinates")
                                target_affine[0:3, 3] = origin
                            if not np.isfinite(target_affine).all():
                                raise ValueError("Affine matrix values must be finite")

                        original_path = os.path.join(work_dir, f"original-{index}{suffix}")
                        updated_path = os.path.join(work_dir, f"updated-{index}{suffix}")
                        shutil.copyfile(local_path, original_path)
                        _save_with_affine(nifti_img, target_affine, updated_path)

                    size, sha256 = _file_identity(updated_path)
                    prepared.append(
                        {
                            **artifact,
                            "original_path": original_path,
                            "updated_path": updated_path,
                            "size": size,
                            "sha256": sha256,
                        }
                    )

                returned_metadata = _metadata_payload(
                    request, nib.load(prepared[0]["updated_path"])
                )
                storage = get_object_storage()
                attempted = []
                # Upload the derived segmentation first; if it fails, the display
                # volume remains untouched. Any attempted writes are then restored.
                upload_order = sorted(
                    prepared, key=lambda artifact: artifact["role"] == "volume"
                )
                try:
                    for artifact in upload_order:
                        attempted.append(artifact)
                        storage.upload_file(
                            artifact["updated_path"],
                            key=artifact["path"],
                            content_type="application/octet-stream",
                        )
                except Exception as upload_error:
                    logger.error("Metadata artifact upload failed: %s", upload_error)
                    rollback_failures = _restore_uploaded_objects(storage, attempted)
                    if rollback_failures:
                        logger.critical(
                            "Metadata upload rollback was incomplete: %s",
                            "; ".join(rollback_failures),
                        )
                    detail = " rollback was incomplete" if rollback_failures else " originals restored"
                    return JsonResponse(
                        {"error": f"Failed to upload updated NIFTI files;{detail}"},
                        status=500,
                    )

                try:
                    with transaction.atomic():
                        _update_file_identities(prepared)
                        # The legacy half only. The arch in `annotations` needs no
                        # cleanup: every revision is stamped with its targets'
                        # content hashes, and `_update_file_identities` has just
                        # changed this volume's -- so the stored arch already reads
                        # as describing bytes that no longer exist, and the next
                        # save starts from revision 0. Deleting it would throw away
                        # the record of what the exported strips were baked from.
                        PanoramicState.objects.filter(patient=patient).delete()
                        Job.objects.create(
                            domain="maxillo",
                            patient=patient,
                            modality_slug="metadata_update",
                            status="completed",
                            output_files={
                                "updated_by": request.user.username,
                                "changes": {
                                    "origin": new_origin,
                                    "affine": new_affine is not None,
                                },
                                "artifacts": {
                                    artifact["role"]: {
                                        "path": artifact["path"],
                                        "sha256": artifact["sha256"],
                                        "file_size": artifact["size"],
                                    }
                                    for artifact in prepared
                                },
                            },
                        )
                except Exception:
                    rollback_failures = _restore_uploaded_objects(storage, prepared)
                    if rollback_failures:
                        logger.critical(
                            "Metadata database rollback could not restore objects: %s",
                            "; ".join(rollback_failures),
                        )
                    raise

                return JsonResponse(returned_metadata)

        except ValueError as e:
            return JsonResponse({"error": f"Invalid metadata: {e}"}, status=400)
        except Exception as e:
            logger.error("Error updating NIFTI metadata: %s", e)
            return JsonResponse(
                {"error": f"Error updating NIFTI file: {str(e)}"}, status=500
            )

    except Exception as e:
        logger.error(f"Error in update_nifti_metadata: {e}")
        return JsonResponse({"error": str(e)}, status=500)
