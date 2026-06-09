from common.models import Project, ProjectAccess


def current_project(request):
    project = None
    icon = ''
    name = ''
    description = ''
    all_projects = []
    pid = request.session.get('current_project_id')
    if pid:
        try:
            project = Project.objects.get(id=pid, is_active=True)
            name = getattr(project, 'name', '') or ''
            icon = getattr(project, 'icon', '') or ''
            description = getattr(project, 'description', '') or ''
        except Project.DoesNotExist:
            pass

    # Expose projects based on user access for navbar switching
    user = getattr(request, 'user', None)
    try:
        if user and user.is_authenticated:
            # Admins can see all projects
            # Use getattr checks to avoid attribute errors if profile is missing
            if user.is_staff or getattr(getattr(user, 'profile', None), 'is_admin', False):
                all_projects = Project.objects.filter(is_active=True).order_by('name')
            else:
                # Regular users only see projects they have access to
                accessible_project_ids = ProjectAccess.objects.filter(
                    user=user
                ).values_list('project_id', flat=True)
                all_projects = Project.objects.filter(
                    is_active=True,
                    id__in=accessible_project_ids
                ).order_by('name')
    except Exception:
        # Avoid breaking templates if profile or db access fails in edge cases
        all_projects = []

    # Determine project-specific role display for the current user
    current_project_slug = ''
    current_project_role_display = None
    current_project_profile = None

    if project and user and user.is_authenticated:
        current_project_slug = getattr(project, 'slug', '') or ''
        try:
            # Try to get profile from request (set by middleware)
            if hasattr(user, 'profile'):
                current_project_profile = user.profile
                current_project_role_display = user.profile.get_role_display()
            else:
                # Fallback: look up ProjectAccess directly
                access = ProjectAccess.objects.filter(
                    user=user,
                    project=project
                ).first()
                if access:
                    current_project_profile = access
                    current_project_role_display = access.get_role_display()
        except Exception:
            current_project_role_display = None

    return {
        'current_project': project,
        'current_project_name': name,
        'current_project_icon': icon,
        'current_project_description': description,
        'current_project_id': pid,
        'all_projects': all_projects,
        'current_project_slug': current_project_slug,
        'current_project_role_display': current_project_role_display,
        'current_project_profile': current_project_profile,
    }
