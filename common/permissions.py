"""Centralized project and folder ACL permission checks."""

from django.apps import apps

from common.models import Project, ProjectAccess


def _namespace(request_or_namespace):
    if isinstance(request_or_namespace, str):
        return request_or_namespace if request_or_namespace in {"maxillo", "brain", "laparoscopy"} else "maxillo"
    namespace = (
        getattr(request_or_namespace, "resolver_match", None)
        and request_or_namespace.resolver_match.namespace
    ) or "maxillo"
    return request_or_namespace if request_or_namespace in {"maxillo", "brain", "laparoscopy"} else "maxillo"


def _folder_access_model(namespace):
    app_label = _namespace(namespace)
    return apps.get_model(app_label, "FolderAccess")


def user_is_project_admin(user, project_or_app_context):
    if not user or not user.is_authenticated:
        return False
    if user.is_staff:
        return True

    if isinstance(project_or_app_context, Project):
        project = project_or_app_context
    else:
        namespace = _namespace(project_or_app_context)
        project = Project.objects.filter(slug=namespace).first()
        if not project:
            return False

    access = ProjectAccess.objects.filter(user=user, project=project).first()
    return bool(access and access.role == "admin")


def get_user_folder_role(user, folder):
    if not user or not user.is_authenticated or not folder:
        return None
    namespace = folder._meta.app_label
    FolderAccess = _folder_access_model(namespace)
    row = FolderAccess.objects.filter(user=user, folder=folder).only("role").first()
    return row.role if row else None


def user_can_read_folder(user, folder, project_or_app_context=None):
    if user_is_project_admin(user, project_or_app_context or folder._meta.app_label):
        return True
    return get_user_folder_role(user, folder) in {"standard", "annotator", "project_manager"}


def user_can_write_annotations(user, folder, project_or_app_context=None):
    if user_is_project_admin(user, project_or_app_context or folder._meta.app_label):
        return True
    return get_user_folder_role(user, folder) in {"annotator", "project_manager"}


def user_can_delete_single_patient(user, folder, project_or_app_context=None):
    return user_can_write_annotations(user, folder, project_or_app_context)


def user_can_move_patient(user, patient):
    return user_is_project_admin(user, patient._meta.app_label)


def user_can_perform_bulk_operations(user, folder_or_project):
    return user_is_project_admin(user, folder_or_project)


def user_can_edit_metadata(user, patient_or_folder):
    return user_is_project_admin(user, patient_or_folder)


def user_can_manage_folder_access(user, folder):
    return user_is_project_admin(user, folder)


def user_can_create_export(user, folder, project_or_app_context=None):
    if user_is_project_admin(user, project_or_app_context or folder._meta.app_label):
        return True
    return get_user_folder_role(user, folder) == "project_manager"


def user_can_download_export(user, export):
    if not user or not user.is_authenticated:
        return False
    if getattr(export, "share_mode", None) == "authenticated":
        return True
    return export.user_id == user.id or user_is_project_admin(user, "maxillo") or user_is_project_admin(user, "brain")


def user_can_edit_caption(user, caption):
    if not user or not user.is_authenticated:
        return False
    if caption.user_id == user.id:
        return True
    return user_is_project_admin(user, caption._meta.app_label)


def user_can_view_caption_content(user, caption, project_or_app_context=None):
    if not user or not user.is_authenticated:
        return False
    if caption.user_id == user.id:
        return True
    if user_is_project_admin(user, project_or_app_context or caption._meta.app_label):
        return True

    patient = getattr(caption, "patient", None)
    folder = getattr(patient, "folder", None) if patient else None
    return get_user_folder_role(user, folder) in {"standard", "project_manager"}


def user_can_delete_caption(user, caption):
    return user_can_edit_caption(user, caption)


def filter_folders_for_user(user, folders_qs, app_label):
    if user_is_project_admin(user, app_label):
        return folders_qs
    FolderAccess = _folder_access_model(app_label)
    folder_ids = FolderAccess.objects.filter(user=user).values_list("folder_id", flat=True)
    return folders_qs.filter(id__in=folder_ids)


def filter_patients_for_user(user, patients_qs, app_label):
    if user_is_project_admin(user, app_label):
        return patients_qs
    FolderAccess = _folder_access_model(app_label)
    folder_ids = FolderAccess.objects.filter(user=user).values_list("folder_id", flat=True)
    return patients_qs.filter(folders__id__in=folder_ids).distinct()


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
        return self.is_admin()

    def is_project_manager(self):
        return False

    def is_student_developer(self):
        return False

    def can_upload_scans(self):
        return bool(self.access)

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
