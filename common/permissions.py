"""Centralized project-scoped ACL permission checks.

Access is granted per Project via ``ProjectAccess`` with roles
``viewer`` / ``annotator`` / ``admin``. Patients and folders live inside a
project, so every check resolves the patient's/folder's project and consults
``ProjectAccess``. The legacy ``FolderAccess`` tables are kept for data
preservation but are no longer read for authorization; the ``is_demo`` flag
that governs the public guest demo still lives on ``Folder``.
"""

from django.apps import apps

from common.domains import normalize_domain
from common.models import Project, ProjectAccess

WRITE_ROLES = {"annotator", "admin"}
READ_ROLES = {"viewer", "annotator", "admin"}


def _namespace(request_or_namespace):
    if isinstance(request_or_namespace, str):
        return normalize_domain(request_or_namespace)
    namespace = (
        getattr(request_or_namespace, "resolver_match", None)
        and request_or_namespace.resolver_match.namespace
    )
    return normalize_domain(namespace)


def _project_from_context(project_or_app_context):
    """Resolve a Project from a Project, a request, or a namespace string.

    For a request the session's current project wins (when it belongs to the
    request's domain); otherwise the domain's entry project for the requesting
    user is used, so this agrees with the project the middleware put them in.
    """
    if isinstance(project_or_app_context, Project):
        return project_or_app_context
    namespace = _namespace(project_or_app_context)
    session = getattr(project_or_app_context, "session", None)
    if session is not None:
        pid = session.get("current_project_id")
        if pid:
            project = Project.objects.filter(
                id=pid, domain=namespace, is_active=True
            ).first()
            if project:
                return project
    return entry_project_for(getattr(project_or_app_context, "user", None), namespace)


def entry_project_for(user, domain):
    """The project ``user`` works in when they enter ``domain``.

    Their first accessible project of the domain, by name. A user with no
    ``ProjectAccess`` in the domain falls back to its first active project --
    they are then refused by the normal checks rather than resolving to no
    project at all.
    """
    active = Project.objects.filter(
        domain=normalize_domain(domain), is_active=True
    ).order_by("name")
    if user is not None and getattr(user, "is_authenticated", False):
        accessible = active.filter(
            id__in=ProjectAccess.objects.filter(user=user).values_list(
                "project_id", flat=True
            )
        ).first()
        if accessible is not None:
            return accessible
    return active.first()


def _access_for(user, project):
    if not user or not user.is_authenticated or project is None:
        return None
    return ProjectAccess.objects.filter(user=user, project=project).first()


def user_is_project_admin(user, project_or_app_context):
    if not user or not user.is_authenticated:
        return False
    if user.is_staff:
        return True
    project = _project_from_context(project_or_app_context)
    if project is None:
        return False
    access = _access_for(user, project)
    return bool(access and access.role == "admin")


def user_has_project_access(user, project_or_app_context):
    if not user or not user.is_authenticated:
        return False
    if user.is_staff:
        return True
    project = _project_from_context(project_or_app_context)
    if project is None:
        return False
    access = _access_for(user, project)
    return bool(access and access.role in READ_ROLES)


def user_can_read_folder(user, folder, project_or_app_context=None):
    from common.demo import is_demo_guest
    if is_demo_guest(user):
        # The public-demo guest can read a folder iff it is flagged is_demo.
        return bool(folder and getattr(folder, "is_demo", False))
    project = (
        _project_from_context(project_or_app_context)
        if project_or_app_context is not None
        else getattr(folder, "project", None)
    )
    if user_is_project_admin(user, project):
        return True
    access = _access_for(user, project)
    return bool(access and access.role in READ_ROLES)


def user_can_write_annotations(user, folder, project_or_app_context=None):
    from common.demo import is_demo_guest
    if is_demo_guest(user):
        return False
    project = (
        _project_from_context(project_or_app_context)
        if project_or_app_context is not None
        else getattr(folder, "project", None)
    )
    if user_is_project_admin(user, project):
        return True
    access = _access_for(user, project)
    return bool(access and access.role in WRITE_ROLES)


def user_can_read_patient(user, patient):
    """Project-scoped read check for a patient (any role)."""
    if not user or not user.is_authenticated or patient is None:
        return False
    return user_can_read_folder(user, getattr(patient, "folder", None), patient.project)


def user_can_write_patient_annotations(user, patient):
    """Project-scoped write check for a patient (annotator/admin)."""
    if not user or not user.is_authenticated or patient is None:
        return False
    return user_can_write_annotations(user, getattr(patient, "folder", None), patient.project)


def project_allows_annotation(patient, method_slug):
    """Whether the patient's project enables an annotation method.

    Absent a project (legacy rows) we stay permissive so nothing breaks; once a
    project exists the annotation-method set is authoritative (UI hides the
    tools and the write endpoints reject them).
    """
    project = getattr(patient, "project", None)
    if project is None:
        return True
    return project.allows_annotation(method_slug)


def user_can_delete_single_patient(user, folder, project_or_app_context=None):
    return user_can_write_annotations(user, folder, project_or_app_context)


def user_can_move_patient(user, patient):
    return user_is_project_admin(user, getattr(patient, "project", None) or patient)


def user_can_perform_bulk_operations(user, folder_or_project):
    return user_is_project_admin(user, folder_or_project)


def user_can_edit_metadata(user, patient_or_folder):
    project = getattr(patient_or_folder, "project", None) or patient_or_folder
    return user_is_project_admin(user, project)


def user_can_create_export(user, folder, project_or_app_context=None):
    from common.demo import is_demo_guest
    if is_demo_guest(user):
        return False
    project = (
        _project_from_context(project_or_app_context)
        if project_or_app_context is not None
        else getattr(folder, "project", None)
    )
    if user_is_project_admin(user, project):
        return True
    access = _access_for(user, project)
    return bool(access and access.role in WRITE_ROLES)


def user_can_download_export(user, export):
    if not user or not user.is_authenticated:
        return False
    if getattr(export, "share_mode", None) == "authenticated":
        return True
    if export.user_id == user.id or user.is_staff:
        return True
    patient = getattr(export, "patient", None)
    return bool(patient and user_is_project_admin(user, patient.project))


def user_can_edit_caption(user, caption):
    if not user or not user.is_authenticated:
        return False
    if caption.user_id == user.id:
        return True
    patient = getattr(caption, "patient", None)
    return bool(patient and user_is_project_admin(user, patient.project))


def user_can_view_caption_content(user, caption, project_or_app_context=None):
    if not user or not user.is_authenticated:
        return False
    if caption.user_id == user.id:
        return True
    patient = getattr(caption, "patient", None)
    if patient is None:
        return False
    if user_is_project_admin(user, patient.project):
        return True
    access = _access_for(user, patient.project)
    if not access:
        return False
    # Annotators see only their own captions (bias guard); viewers and admins
    # see everything in the project.
    if access.role == "annotator":
        return False
    return access.role in READ_ROLES


def user_can_delete_caption(user, caption):
    return user_can_edit_caption(user, caption)


def filter_folders_for_user(user, folders_qs, app_label):
    from common.demo import is_demo_guest
    if is_demo_guest(user):
        return folders_qs.filter(is_demo=True)
    if user and user.is_staff:
        return folders_qs
    project_ids = ProjectAccess.objects.filter(user=user).values_list(
        "project_id", flat=True
    )
    return folders_qs.filter(project_id__in=project_ids)


def filter_patients_for_user(user, patients_qs, app_label):
    from common.demo import demo_patients, is_demo_guest
    if is_demo_guest(user):
        patient_ids = list(demo_patients(app_label).values_list("pk", flat=True))
        return patients_qs.filter(pk__in=patient_ids)
    if user and user.is_staff:
        return patients_qs
    project_ids = ProjectAccess.objects.filter(user=user).values_list(
        "project_id", flat=True
    )
    return patients_qs.filter(project_id__in=project_ids)


class PermissionChecker:
    """Compatibility wrapper around project-level access only."""

    def __init__(self, user, project):
        self.user = user
        self.project = project
        self._access = None

    @property
    def access(self):
        if self._access is None and self.user and self.user.is_authenticated and self.project:
            self._access = ProjectAccess.objects.filter(user=self.user, project=self.project).first()
        return self._access

    @property
    def role(self):
        return self.access.role if self.access else None

    def is_admin(self):
        return bool(self.access and self.access.role == "admin")

    def is_annotator(self):
        return bool(self.access and self.access.role in {"annotator", "admin"})

    def is_project_manager(self):
        return False

    def is_student_developer(self):
        return False

    def can_upload_scans(self):
        return bool(self.access and self.access.role in {"annotator", "admin"})

    def can_see_debug_scans(self):
        return self.is_admin()

    def can_see_public_private_scans(self):
        return bool(self.access)

    def can_modify_scan_settings(self):
        return self.is_admin()

    def can_delete_scans(self):
        return self.is_admin()

    def can_delete_debug_scans(self):
        return self.is_admin()

    def can_view_other_profiles(self):
        return self.is_admin()

    def get_role_display(self):
        return self.access.get_role_display() if self.access else "No Access"
