"""
Views package for maxillo app.

This package re-exports all views from submodules for backwards compatibility.
"""

# Helper functions
from .helpers import render_with_fallback, redirect_with_namespace

# Authentication and invitations
from .auth import register, invitation_list, delete_invitation

# Patient list and project selection
from .patient_list import home, select_project, patient_list

# Patient upload
from .patient_upload import upload_patient

# Classification
from .classification import update_classification

# Folder and tag management
from .folders_tags import (
    create_folder,
    move_patients_to_folder,
    add_patient_tag,
    remove_patient_tag,
    folder_stats,
    folder_permissions,
    upsert_folder_permission,
    delete_folder_permission,
    rename_folder,
)

# Deletion
from .deletion import delete_patient, bulk_delete_patients

# Patient detail and management
from .patient_detail import patient_detail, update_patient_name
from .file_management import add_raw_file, delete_raw_file

# Patient data API endpoints
from .patient_data import (
    patient_viewer_data,
    patient_cbct_data,
    patient_volume_data,
    patient_panoramic_data,
    patient_intraoral_data,
    patient_teleradiography_data,
    patient_panoramic_data,
)

# Intraoral segmentation APIs
from .intraoral_segmentation import (
    patient_intraoral_segmentation_data,
    update_patient_intraoral_segmentation,
)

# Voice captions
from .voice_captions import (
    upload_voice_caption,
    delete_voice_caption,
    upload_text_caption,
    edit_voice_caption_transcription,
    update_voice_caption_modality,
)

# Admin
from .admin import rerun_processing, bulk_rerun_processing, admin_control_panel

# Metadata
from .metadata import get_nifti_metadata, update_nifti_metadata

# Profile
from .profile import user_profile

# Export
from .export import (
    export_list,
    export_new,
    export_preview,
    export_status,
    export_download,
    export_share_update,
    export_shared_landing,
    export_shared_download,
    export_delete,
    export_stop,
)

# Export all functions
__all__ = [
    # Helpers
    'render_with_fallback',
    'redirect_with_namespace',
    # Auth
    'register',
    'invitation_list',
    'delete_invitation',
    # Patient list
    'home',
    'select_project',
    'patient_list',
    # Upload
    'upload_patient',
    # Detail
    'patient_detail',
    'update_patient_name',
    'add_raw_file',
    'delete_raw_file',
    # Classification
    'update_classification',
    # Data APIs
    'patient_viewer_data',
    'patient_cbct_data',
    'patient_volume_data',
    'patient_panoramic_data',
    'patient_intraoral_data',
    'patient_teleradiography_data',
    'patient_panoramic_data',
    'patient_intraoral_segmentation_data',
    'update_patient_intraoral_segmentation',
    # Voice captions
    'upload_voice_caption',
    'delete_voice_caption',
    'upload_text_caption',
    'edit_voice_caption_transcription',
    'update_voice_caption_modality',
    # Deletion
    'delete_patient',
    'bulk_delete_patients',
    # Admin
    'rerun_processing',
    'bulk_rerun_processing',
    'admin_control_panel',
    # Metadata
    'get_nifti_metadata',
    'update_nifti_metadata',
    # Folders and tags
    'create_folder',
    'move_patients_to_folder',
    'add_patient_tag',
    'remove_patient_tag',
    'folder_stats',
    'folder_permissions',
    'upsert_folder_permission',
    'delete_folder_permission',
    'rename_folder',
    # Profile
    'user_profile',
    # Export
    'export_list',
    'export_new',
    'export_preview',
    'export_status',
    'export_download',
    'export_share_update',
    'export_shared_landing',
    'export_shared_download',
    'export_delete',
    'export_stop',
]
