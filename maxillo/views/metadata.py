"""NIFTI metadata management views."""

from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
import json
import logging

from common.file_access import exists as artifact_exists
from common.object_storage import download_to_tempfile, get_object_storage

from .domain import get_domain_models, get_namespace

logger = logging.getLogger(__name__)


@login_required
def get_nifti_metadata(request, patient_id):
    """Get NIFTI metadata including origin, affine matrix, and orientation"""
    try:
        Patient = get_domain_models(request)["Patient"]
        patient = get_object_or_404(Patient, patient_id=patient_id)
        user_profile = request.user.profile

        # Check permissions based on scan visibility and user role
        can_view = False
        if user_profile.is_admin():
            can_view = True
        elif user_profile.is_annotator() and patient.visibility != "debug":
            can_view = True
        elif user_profile.is_student_developer() and patient.visibility == "debug":
            can_view = True
        elif patient.visibility == "public":
            can_view = True

        if not can_view:
            return JsonResponse({"error": "Permission denied"}, status=403)

        # Check if CBCT exists
        if not patient.has_cbct_scan():
            return JsonResponse({"error": "No CBCT scan available"}, status=404)

        # Get CBCT file path - prioritize processed NIFTI files
        cbct_path = None

        # First, try to get processed CBCT (converted .nii.gz)
        try:
            processed_entry = patient.files.filter(file_type="cbct_processed").first()
            if processed_entry:
                if (
                    processed_entry.file_hash == "multi-file"
                    and "files" in processed_entry.metadata
                ):
                    # New structure: look for converted volume in metadata
                    files_data = processed_entry.metadata.get("files", {})
                    volume_data = files_data.get("volume_nifti", {})
                    volume_path = volume_data.get("path")
                    if volume_path and artifact_exists(volume_path):
                        cbct_path = volume_path
        except:
            pass

        # Fallback to raw CBCT if no processed version available
        if not cbct_path:
            try:
                # Try to get from FileRegistry first
                cbct_entry = patient.files.filter(file_type="cbct_raw").first()
                if cbct_entry and artifact_exists(cbct_entry.file_path):
                    # Only use raw file if it's already in .nii.gz format
                    if cbct_entry.file_path.endswith(".nii.gz"):
                        cbct_path = cbct_entry.file_path
                    else:
                        # Raw file is a directory or non-NIFTI file, check if processing is needed
                        if (cbct_entry.metadata or {}).get(
                            "upload_type"
                        ) == "folder" or (cbct_entry.metadata or {}).get(
                            "file_format"
                        ) == "dicom_folder":
                            return JsonResponse(
                                {
                                    "error": "CBCT needs to be processed first",
                                    "status": "needs_processing",
                                    "message": "The CBCT volume is in DICOM format and needs to be converted to NIFTI. Please wait for processing to complete.",
                                },
                                status=202,
                            )
                        else:
                            return JsonResponse(
                                {"error": "CBCT file is not in NIFTI format"},
                                status=400,
                            )
            except:
                pass

        if not cbct_path or not artifact_exists(cbct_path):
            return JsonResponse({"error": "CBCT file not found"}, status=404)

        # Load NIFTI file and extract metadata
        import nibabel as nib
        import numpy as np

        try:
            suffix = ".nii.gz" if cbct_path.endswith(".nii.gz") else ".nii"
            with download_to_tempfile(cbct_path, suffix=suffix) as tmp_path:
                nifti_img = nib.load(tmp_path)

            # Get header information
            header = nifti_img.header

            # Get affine matrix safely
            try:
                affine = nifti_img.affine.tolist()
                # Validate affine matrix structure
                if not affine or not isinstance(affine, list) or len(affine) != 4:
                    raise ValueError("Invalid affine matrix structure")
                for row in affine:
                    if not isinstance(row, list) or len(row) != 4:
                        raise ValueError("Invalid affine matrix row structure")
            except Exception as affine_error:
                logger.error(f"Error processing affine matrix: {affine_error}")
                # Create a default identity matrix as fallback
                affine = [
                    [1.0, 0.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0, 0.0],
                    [0.0, 0.0, 1.0, 0.0],
                    [0.0, 0.0, 0.0, 1.0],
                ]

            # Get voxel dimensions safely
            try:
                voxel_dims = header.get_zooms()[:3]
                if not voxel_dims or len(voxel_dims) < 3:
                    voxel_dims = [1.0, 1.0, 1.0]  # Default voxel size
            except:
                voxel_dims = [1.0, 1.0, 1.0]  # Default voxel size

            # Get data shape safely
            try:
                shape = (
                    nifti_img.shape[:3]
                    if len(nifti_img.shape) >= 3
                    else nifti_img.shape
                )
                if not shape or len(shape) < 3:
                    shape = [1, 1, 1]  # Default shape
            except:
                shape = [1, 1, 1]  # Default shape

            # Get orientation safely
            try:
                from nibabel.orientations import aff2axcodes

                orientation_codes = aff2axcodes(nifti_img.affine)
                orientation = (
                    "".join(orientation_codes) if orientation_codes else "unknown"
                )
            except:
                orientation = "unknown"

            # Get units safely
            try:
                xyzt_units = header.get_xyzt_units()
                spatial_unit = (
                    str(xyzt_units[0])
                    if xyzt_units and len(xyzt_units) > 0 and xyzt_units[0]
                    else "unknown"
                )
                temporal_unit = (
                    str(xyzt_units[1])
                    if xyzt_units and len(xyzt_units) > 1 and xyzt_units[1]
                    else "unknown"
                )
            except:
                spatial_unit = "unknown"
                temporal_unit = "unknown"

            # Get description safely
            try:
                description = (
                    str(header.get("descrip", "")) if header.get("descrip") else ""
                )
            except:
                description = ""

            # Additional metadata with explicit type conversion
            try:
                # Ensure all values are JSON-serializable
                data_type_str = str(header.get_data_dtype())
                can_edit_bool = bool(user_profile.is_admin)

                metadata = {
                    "affine": affine,  # Already converted to list by .tolist()
                    "orientation": str(orientation),  # Ensure it's a string
                    "voxel_dimensions": [
                        float(x) for x in voxel_dims
                    ],  # Convert to native Python floats
                    "shape": [int(x) for x in shape],  # Convert to native Python ints
                    "data_type": data_type_str,
                    "units": {
                        "spatial": str(spatial_unit),
                        "temporal": str(temporal_unit),
                    },
                    "description": str(description),
                    "can_edit": can_edit_bool,
                }

                return JsonResponse(metadata)
            except Exception as metadata_error:
                logger.error(f"Error creating metadata dictionary: {metadata_error}")
                # Return a simplified metadata structure as fallback
                try:
                    fallback_metadata = {
                        "affine": affine,
                        "orientation": "unknown",
                        "voxel_dimensions": [float(x) for x in voxel_dims],
                        "shape": [int(x) for x in shape],
                        "data_type": "unknown",
                        "units": {"spatial": "unknown", "temporal": "unknown"},
                        "description": "",
                        "can_edit": bool(user_profile.is_admin),
                    }
                    return JsonResponse(fallback_metadata)
                except Exception as fallback_error:
                    logger.error(f"Error creating fallback metadata: {fallback_error}")
                    return JsonResponse(
                        {"error": "Failed to create metadata"}, status=500
                    )

        except Exception as e:
            logger.error(f"Error loading NIFTI metadata: {e}")
            return JsonResponse(
                {"error": f"Error loading NIFTI file: {str(e)}"}, status=500
            )

    except Exception as e:
        logger.error(f"Error getting NIFTI metadata: {e}")
        return JsonResponse({"error": str(e)}, status=500)


@login_required
def update_nifti_metadata(request, patient_id):
    """Update NIFTI metadata (admin only)"""
    try:
        Patient = get_domain_models(request)["Patient"]
        patient = get_object_or_404(Patient, patient_id=patient_id)
        domain = get_namespace(request)

        # Check if CBCT exists
        if not patient.has_cbct_scan():
            return JsonResponse({"error": "No CBCT scan available"}, status=404)

        cbct_path = None

        try:
            processed_entry = patient.files.filter(file_type="cbct_processed").first()
            if processed_entry:
                if (
                    processed_entry.file_hash == "multi-file"
                    and "files" in processed_entry.metadata
                ):
                    files_data = processed_entry.metadata.get("files", {})
                    volume_data = files_data.get("volume_nifti", {})
                    volume_path = volume_data.get("path")
                    if volume_path and artifact_exists(volume_path):
                        cbct_path = volume_path
        except:
            pass

        if not cbct_path:
            try:
                cbct_entry = patient.files.filter(file_type="cbct_raw").first()
                if cbct_entry and artifact_exists(cbct_entry.file_path):
                    if cbct_entry.file_path.endswith(".nii.gz"):
                        cbct_path = cbct_entry.file_path
                    else:
                        if (cbct_entry.metadata or {}).get(
                            "upload_type"
                        ) == "folder" or (cbct_entry.metadata or {}).get(
                            "file_format"
                        ) == "dicom_folder":
                            return JsonResponse(
                                {
                                    "error": "CBCT needs to be processed first",
                                    "status": "needs_processing",
                                    "message": "The CBCT volume is in DICOM format and needs to be converted to NIFTI before metadata can be updated.",
                                },
                                status=202,
                            )
                        else:
                            return JsonResponse(
                                {"error": "CBCT file is not in NIFTI format"},
                                status=400,
                            )
            except:
                pass

        if not cbct_path or not artifact_exists(cbct_path):
            return JsonResponse({"error": "CBCT file not found"}, status=404)

        # Parse request data
        data = json.loads(request.body)
        new_origin = data.get("origin")
        new_affine = data.get("affine")

        if not new_origin and not new_affine:
            return JsonResponse({"error": "No metadata to update"}, status=400)

        # Load NIFTI file
        import nibabel as nib
        import numpy as np

        try:
            suffix = ".nii.gz" if cbct_path.endswith(".nii.gz") else ".nii"
            with download_to_tempfile(cbct_path, suffix=suffix) as local_path:
                nifti_img = nib.load(local_path)
                current_affine = nifti_img.affine.copy()

                if new_affine:
                    try:
                        new_affine_array = np.array(new_affine, dtype=np.float64)
                        if new_affine_array.shape != (4, 4):
                            raise ValueError("Affine matrix must be 4x4")
                        current_affine = new_affine_array
                    except Exception as e:
                        return JsonResponse(
                            {"error": f"Invalid affine matrix: {str(e)}"},
                            status=400,
                        )

                elif new_origin:
                    try:
                        if len(new_origin) != 3:
                            raise ValueError("Origin must have 3 coordinates")
                        current_affine[0:3, 3] = new_origin
                    except Exception as e:
                        return JsonResponse(
                            {"error": f"Invalid origin: {str(e)}"}, status=400
                        )

                new_nifti = nib.Nifti1Image(
                    nifti_img.get_fdata(), current_affine, nifti_img.header
                )
                nib.save(new_nifti, local_path)

                get_object_storage().upload_file(
                    local_path,
                    key=cbct_path,
                    content_type="application/octet-stream",
                )

            from common.models import Job

            if domain == "brain":
                Job.objects.create(
                    domain="brain",
                    brain_patient=patient,
                    modality_slug="metadata_update",
                    status="completed",
                    output_files={
                        "updated_by": request.user.username,
                        "changes": {
                            "origin": new_origin,
                            "affine": new_affine is not None,
                        },
                    },
                )
            else:
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
                    },
                )

            # Return updated metadata
            return get_nifti_metadata(request, patient_id)

        except Exception as e:
            logger.error(f"Error updating NIFTI metadata: {e}")
            return JsonResponse(
                {"error": f"Error updating NIFTI file: {str(e)}"}, status=500
            )

    except Exception as e:
        logger.error(f"Error in update_nifti_metadata: {e}")
        return JsonResponse({"error": str(e)}, status=500)
