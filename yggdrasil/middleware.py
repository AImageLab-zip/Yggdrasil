import logging
import time
from common import presence
from common.domains import DOMAINS
from common.models import Project, ProjectAccess
from common.permissions import entry_project_for
from django.utils.deprecation import MiddlewareMixin
import traceback
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect

logger = logging.getLogger(__name__)


class RequestLoggingMiddleware(MiddlewareMixin):
    """Request/response access logging.

    Deliberately logs metadata only -- never headers, never request bodies.
    An earlier version dumped ``dict(request.headers)`` and every write body at
    DEBUG, which put live session cookies and runner bearer tokens into
    ``logs/django.log`` in plaintext. Anything added here must stay on the
    metadata side of that line: this logger's output is retained on disk, and a
    header dump is a credential store.
    """

    def process_request(self, request):
        request.start_time = time.time()
        logger.info(f"Request: {request.method} {request.path}")

    def process_response(self, request, response):
        if hasattr(request, "start_time"):
            duration = time.time() - request.start_time
            logger.info(
                f"Response: {request.method} {request.path} - "
                f"{response.status_code} ({duration:.3f}s)"
            )
        else:
            logger.info(
                f"Response: {request.method} {request.path} - {response.status_code}"
            )

        if response.status_code >= 400:
            logger.warning(
                f"Error response for {request.method} {request.path}: "
                f"{response.status_code}"
            )

        return response

    def process_exception(self, request, exception):
        """Log unhandled exceptions."""
        logger.error(
            f"Unhandled exception for {request.method} {request.path}: {exception}"
        )
        logger.error(f"Exception type: {type(exception).__name__}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        return None


class ProjectSessionMiddleware(MiddlewareMixin):
    """
    Middleware that keeps ``current_project_id`` pointing at a project of the
    domain being browsed.

    The session project is domain-scoped, so it must be re-resolved when the
    user crosses into another domain -- keeping the previous domain's project
    left every downstream check (and ``ActiveProfileMiddleware``) resolving the
    wrong project and bouncing the user back to the landing page.
    """

    def process_request(self, request):
        """Set project session based on URL path"""
        if getattr(request, "maintenance_mode", "normal") != "normal" and not getattr(request.user, "is_staff", False):
            return None
        if not request.user.is_authenticated:
            return None
        if not request.path.startswith('/'):
            return None

        url_start = request.path.split('/')[1]
        if url_start not in DOMAINS:
            return None

        pid = request.session.get('current_project_id')
        if pid and Project.objects.filter(id=pid, domain=url_start, is_active=True).exists():
            return None

        # Multiple projects live under one domain; enter at the first the user
        # can actually access.
        project = entry_project_for(request.user, url_start)
        if project is None:
            return None
        request.session['current_project_id'] = project.id

        return None


class ActiveProfileMiddleware(MiddlewareMixin):
    """
    Middleware that sets `request.user.profile` to the ProjectAccess object
    for the current project. This maintains backward compatibility with
    existing view and template code that uses `user.profile`.

    The profile is resolved from ProjectAccess based on the URL path
    (maxillo or brain) and the user's access to that project.

    After this middleware runs:
    - request.user.profile = ProjectAccess object (has same methods as old UserProfile)
    - request.user_role = role string ('admin', 'annotator', etc.)
    - request.user_project_access = ProjectAccess object (explicit reference)
    """

    def process_request(self, request):
        # Only operate for authenticated users
        if not hasattr(request, 'user') or not request.user.is_authenticated:
            return None

        path_parts = [p for p in request.path.split('/') if p]

        if not path_parts:
            return None

        app_key = path_parts[0]

        if app_key not in DOMAINS:
            return None

        try:
            # Resolve the project the user is working in: the session project
            # when it belongs to this domain, else the first active project of
            # the domain.
            pid = request.session.get('current_project_id')
            project = None
            if pid:
                project = Project.objects.filter(
                    id=pid, domain=app_key, is_active=True
                ).first()
            if project is None:
                project = entry_project_for(request.user, app_key)
            if project is None:
                logger.warning(f"ActiveProfileMiddleware: No project for domain '{app_key}'")
                return redirect('/')

            # Get ProjectAccess for this user and project
            access = ProjectAccess.objects.select_related('project').get(
                user=request.user,
                project=project
            )

            # Set profile to ProjectAccess (has same interface as old profiles)
            request.user.profile = access

            # Also set explicit attributes for clarity
            request.user_role = access.role
            request.user_project_access = access

        except Project.DoesNotExist:
            logger.warning(f"ActiveProfileMiddleware: Project not found for domain '{app_key}'")
            return redirect('/')
        except ProjectAccess.DoesNotExist:
            if getattr(request.user, 'is_staff', False):
                # Staff (lab developers) are admins everywhere: auto-provision the
                # ProjectAccess row instead of bouncing them out of the app.
                access = ProjectAccess.objects.create(
                    user=request.user, project=project, role='admin'
                )
                request.user.profile = access
                request.user_role = access.role
                request.user_project_access = access
            else:
                logger.debug(f"ActiveProfileMiddleware: No ProjectAccess for user {request.user.id} in project '{app_key}'")
                return redirect('/')
        except Exception as e:
            logger.error(f"ActiveProfileMiddleware: Unexpected error for user {request.user.id} in app '{app_key}': {e}")
            return redirect('/')

        return None


class SiteMaintenanceMiddleware(MiddlewareMixin):
    """Apply global maintenance access rules before application views run."""

    SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
    # "/changelog/" is public because the maintenance page itself shows the version
    # as a link to it; without this, that link would redirect straight back here.
    PUBLIC_PATHS = ("/maintenance/", "/login/", "/logout/", "/healthz", "/changelog/")

    @staticmethod
    def _is_runner_callback(path):
        return "/api/runner/" in path

    @staticmethod
    def _expects_json(request):
        accept = request.headers.get("Accept", "")
        return "/api/" in request.path or "application/json" in accept or request.headers.get("X-Requested-With") == "XMLHttpRequest"

    def process_request(self, request):
        path = request.path
        if path.startswith("/static/") or path in self.PUBLIC_PATHS or path.startswith("/admin/login/"):
            return None
        if self._is_runner_callback(path):
            return None

        from common.models import SiteMaintenance

        try:
            maintenance = SiteMaintenance.get_solo()
        except Exception:
            # A rolling deployment can serve requests before this migration runs.
            return None

        mode = maintenance.access_mode
        request.maintenance_mode = mode
        if mode == SiteMaintenance.MODE_NORMAL or getattr(request.user, "is_staff", False):
            return None

        if mode == SiteMaintenance.MODE_LOCKDOWN:
            if request.method in self.SAFE_METHODS and not self._expects_json(request):
                return redirect("maintenance_page")
            return JsonResponse(
                {"error": "The service is temporarily unavailable for maintenance."},
                status=503,
            )

        if mode == SiteMaintenance.MODE_READ_ONLY and request.method not in self.SAFE_METHODS:
            message = "The site is temporarily read-only for maintenance."
            if self._expects_json(request):
                return JsonResponse({"error": message}, status=423)
            return HttpResponse(message, status=423)
        return None


class DemoGuestReadOnlyMiddleware(MiddlewareMixin):
    """Hard read-only backstop for the shared public-demo guest user.

    The guest holds a standard ProjectAccess so the real @login_required views
    work, but it must NEVER mutate anything. Every write path is a non-safe HTTP
    method, so we reject all of them for the guest here — before any view runs —
    regardless of what per-view permission checks would decide. Logging out (a
    POST) is the one allowed exception. Runs after ActiveProfileMiddleware so
    request.user is populated.
    """

    SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

    def process_request(self, request):
        if request.method in self.SAFE_METHODS:
            return None
        from common.demo import is_demo_guest
        if not is_demo_guest(getattr(request, "user", None)):
            return None
        from django.urls import NoReverseMatch, reverse
        try:
            if request.path == reverse("logout"):
                return None
        except NoReverseMatch:
            pass
        return HttpResponseForbidden("This is a read-only public demo.")


class PresenceMiddleware(MiddlewareMixin):
    """
    Refreshes a short-lived Redis key for every authenticated request,
    used to power the live "who's online" admin dashboard.
    """

    def process_request(self, request):
        if getattr(request, "maintenance_mode", "normal") != "normal" and not getattr(request.user, "is_staff", False):
            return None
        if request.user.is_authenticated:
            presence.touch(request.user, request.path)
        return None
