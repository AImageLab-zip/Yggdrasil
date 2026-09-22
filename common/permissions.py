"""Centralized project-scoped ACL permission checks.

Access is granted per Project via ``ProjectAccess`` with roles
``viewer`` / ``annotator`` / ``admin``. Patients and folders live inside a
project, so every check resolves the patient's/folder's project and consults
``ProjectAccess``. The legacy ``FolderAccess`` tables are kept for data
preservation but are no longer read for authorization.

The public-demo guest is an ordinary user here: it sees a project because it
holds a ``viewer`` ``ProjectAccess`` on it, through the same code path as
everyone else. Granting that role is therefore what publishes a project to the
anonymous demo -- there is no separate demo flag.
"""


from common.domains import normalize_domain
from common.models import Project, ProjectAccess

WRITE_ROLES = {"annotator", "admin"}
READ_ROLES = {"viewer", "annotator", "admin"}


def current_project(request):
    """The project the user is working in for this request's domain.

    The session's ``current_project_id`` when it belongs to the request's domain,
    else the user's entry project (``entry_project_for``). This is a UI
    preference -- which project a list page opens on, where an upload lands --
    and never an authorization input: every object-level check takes the
    object's own project. Passing a request to a permission helper used to mean
    "the session project", so an admin of project A passed checks on patients of
    project B; the helpers now refuse anything that is not a ``Project``.
    """
    from common.domain_models import get_namespace

    namespace = normalize_domain(get_namespace(request))
    session = getattr(request, "session", None)
    if session is not None:
        pid = session.get("current_project_id")
        if pid:
            project = Project.objects.filter(
                id=pid, domain=namespace, is_active=True
            ).first()
            if project:
                return project
    return entry_project_for(getattr(request, "user", None), namespace)


def _as_project(project):
    """``project`` itself, refusing requests and domain names.

    A ``None`` project (a legacy row without one) denies instead of guessing.
    """
    if project is None or isinstance(project, Project):
        return project
    raise TypeError(
        f"permission checks take a Project, not {type(project).__name__}; "
        "use the object's own project (current_project(request) is UI-only)"
    )


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


def user_is_project_admin(user, project):
    project = _as_project(project)
    if not user or not user.is_authenticated:
        return False
    if user.is_staff:
        return True
    if project is None:
        return False
    access = _access_for(user, project)
    return bool(access and access.role == "admin")


def user_has_project_access(user, project):
    project = _as_project(project)
    if not user or not user.is_authenticated:
        return False
    if user.is_staff:
        return True
    if project is None:
        return False
    access = _access_for(user, project)
    return bool(access and access.role in READ_ROLES)


def _project_for_folder(folder, project=None):
    """The project whose ACL decides access to ``folder``.

    A folder's own project is authoritative whenever it has one; ``project`` is
    only the fallback for a missing folder (e.g. a patient outside any folder
    passes its own project).
    """
    folder_project = getattr(folder, "project", None)
    if folder_project is not None:
        return folder_project
    return _as_project(project)


def user_can_read_folder(user, folder, project=None):
    project = _project_for_folder(folder, project)
    if user_is_project_admin(user, project):
        return True
    access = _access_for(user, project)
    return bool(access and access.role in READ_ROLES)


def user_can_write_annotations(user, folder, project=None):
    # A viewer is refused below anyway; this is the cheap guard for the day
    # someone grants the shared guest account a writing role by mistake.
    from common.demo import is_demo_guest
    if is_demo_guest(user):
        return False
    project = _project_for_folder(folder, project)
    if user_is_project_admin(user, project):
        return True
    access = _access_for(user, project)
    return bool(access and access.role in WRITE_ROLES)


def user_can_read_patient(user, patient):
    """Read check for a patient (any role) against the patient's own project."""
    if not user or not user.is_authenticated or patient is None:
        return False
    return user_can_read_folder(user, None, patient.project)


def user_can_write_patient_annotations(user, patient):
    """Write check for a patient (annotator/admin) against the patient's own project."""
    if not user or not user.is_authenticated or patient is None:
        return False
    return user_can_write_annotations(user, None, patient.project)


def user_is_patient_admin(user, patient):
    """Admin check against the patient's own project."""
    return patient is not None and user_is_project_admin(user, patient.project)


PATIENT_PERMISSIONS = {
    "read": user_can_read_patient,
    "write": user_can_write_patient_annotations,
    "admin": user_is_patient_admin,
}


def get_patient_for(user, patient_model, patient_id, perm="read"):
    """Load a patient and authorize ``perm`` on *its* project.

    The one choke point for object-level patient access. Raises ``Http404`` when
    the user cannot read the patient at all -- ids of other projects' patients
    are not confirmed to exist -- and ``PermissionDenied`` (403) when they can
    read it but ``perm`` asks for more.
    """
    from django.core.exceptions import PermissionDenied
    from django.http import Http404

    check = PATIENT_PERMISSIONS[perm]
    patient = patient_model.objects.select_related("project").filter(pk=patient_id).first()
    if patient is None or not user_can_read_patient(user, patient):
        raise Http404("No such patient")
    if not check(user, patient):
        raise PermissionDenied("Permission denied")
    return patient


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


def user_can_delete_single_patient(user, folder, project=None):
    return user_can_write_annotations(user, folder, project)


def user_can_move_patient(user, patient):
    return user_is_project_admin(user, getattr(patient, "project", None))


def user_can_perform_bulk_operations(user, project):
    return user_is_project_admin(user, project)


def user_can_edit_metadata(user, patient_or_folder):
    return user_is_project_admin(user, getattr(patient_or_folder, "project", None))


def user_can_create_export(user, folder, project=None):
    from common.demo import is_demo_guest
    if is_demo_guest(user):
        return False
    project = _project_for_folder(folder, project)
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


def user_can_view_caption_content(user, caption, project=None):
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
    if user and user.is_staff:
        return folders_qs
    project_ids = ProjectAccess.objects.filter(user=user).values_list(
        "project_id", flat=True
    )
    return folders_qs.filter(project_id__in=project_ids)


def filter_patients_for_user(user, patients_qs, app_label):
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
