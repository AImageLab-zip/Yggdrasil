"""
API Views package for maxillo app.

This package re-exports all API views from submodules for backwards compatibility.
"""

# Health check
from .health import health_check

# Jobs
from .jobs import (
    get_job_status,
    ProcessingJobListView,
)

# Runner callbacks
from .runner import (
    runner_claim_job,
    runner_complete_job,
    runner_fail_job,
)

# Files
from .files import serve_file, get_file_registry

# Projects
from .projects import (
    project_upload_api,
    get_project_folders,
    project_patients_handler,
    get_project_patients_and_modalities,
    get_patient_files,
    get_multiple_patients_files,
)

# Export all functions
__all__ = [
    # Health
    "health_check",
    # Jobs
    "get_job_status",
    "ProcessingJobListView",
    # Runner callbacks
    "runner_claim_job",
    "runner_complete_job",
    "runner_fail_job",
    # Files
    "serve_file",
    "get_file_registry",
    # Projects
    "project_upload_api",
    "get_project_folders",
    "project_patients_handler",
    "get_project_patients_and_modalities",
    "get_patient_files",
    "get_multiple_patients_files",
]
