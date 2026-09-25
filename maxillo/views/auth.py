"""Authentication and invitation-related views."""
import logging
import uuid
from email.mime.image import MIMEImage

from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.mail import EmailMultiAlternatives, get_connection, send_mail
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.db.models import Prefetch
from django.contrib import messages
from django.template.loader import render_to_string
from django.utils import timezone
from django.urls import reverse
from django.views.decorators.http import require_POST

from common.mobile import desktop_auth_only
from common.models import Invitation
from ..forms import InvitationForm, InvitedUserCreationForm
from common.models import ProjectAccess   


logger = logging.getLogger(__name__)


def _repair_empty_invitation_codes():
    for invitation in Invitation.objects.filter(code='').only('pk', 'code'):
        invitation.code = str(uuid.uuid4())
        invitation.save(update_fields=['code'])


@desktop_auth_only
def register(request):
    if request.method == 'POST':
        form = InvitedUserCreationForm(request.POST)
        if form.is_valid():
            invitation = Invitation.objects.get(code=form.cleaned_data['invitation_code'])
            user = form.save()

            invitation_projects = list(invitation.projects.all())
            if not invitation_projects and invitation.project:
                invitation_projects = [invitation.project]

            for invitation_project in invitation_projects:
                access, created = ProjectAccess.objects.get_or_create(
                    user=user,
                    project=invitation_project,
                    defaults={
                        'role': invitation.role
                    }
                )
                if not created and access.role != invitation.role:
                    access.role = invitation.role
                    access.save()
            
            invitation.used_at = timezone.now()
            invitation.used_by = user
            invitation.save()

            # Notify admin of new registration
            try:
                registered_at = timezone.localtime(invitation.used_at).strftime('%Y-%m-%d %H:%M %Z')
                project_names = ', '.join(p.name for p in invitation_projects) or '—'
                notification_context = {
                    'email': user.email,
                    'username': user.username,
                    'project_names': project_names,
                    'registered_at': registered_at,
                }
                notification_subject = render_to_string(
                    'registration/emails/new_registration_subject.txt', notification_context
                ).strip()
                notification_message = render_to_string(
                    'registration/emails/new_registration_body.txt', notification_context
                )
                admin_email = settings.DEFAULT_FROM_EMAIL
                connection = get_connection(
                    username=settings.EMAIL_HOST_USER,
                    password=settings.EMAIL_HOST_PASSWORD,
                    fail_silently=False,
                )
                send_mail(
                    notification_subject,
                    notification_message,
                    admin_email,
                    [admin_email],
                    fail_silently=False,
                    connection=connection,
                )
            except Exception:
                logger.error('Failed to send new registration notification for user %s', user.username, exc_info=True)

            messages.success(request, f'Account created for {user.username}!')
            return redirect('login')
    else:
        initial = {}
        if 'code' in request.GET:
            initial['invitation_code'] = request.GET['code']
            try:
                invitation = Invitation.objects.get(code=request.GET['code'])
                if invitation.email:
                    initial['email'] = invitation.email
            except Invitation.DoesNotExist:
                pass
        form = InvitedUserCreationForm(initial=initial)
    return render(request, 'registration/register.html', {'form': form})


INVITATION_LOGO_CID = 'yggdrasil-logo'


def _send_invitation_email(invitation, context, sender_email, connection):
    """Send the invitation as text + HTML, with the logo attached inline (cid:)."""
    subject = render_to_string('registration/emails/invitation_subject.txt', context).strip()
    text_body = render_to_string('registration/emails/invitation_body.txt', context)
    html_body = render_to_string('registration/emails/invitation_body.html', context)

    message = EmailMultiAlternatives(
        subject, text_body, sender_email, [invitation.email], connection=connection,
    )
    message.attach_alternative(html_body, 'text/html')

    logo_path = finders.find('icons/email-logo.png')
    if logo_path:
        # multipart/related keeps clients from listing the inline logo as an attachment.
        message.mixed_subtype = 'related'
        with open(logo_path, 'rb') as fh:
            logo = MIMEImage(fh.read(), 'png')
        logo.add_header('Content-ID', f'<{INVITATION_LOGO_CID}>')
        logo.add_header('Content-Disposition', 'inline', filename='yggdrasil-logo.png')
        message.attach(logo)
    else:
        logger.warning('Invitation email logo not found; sending without it')

    message.send(fail_silently=False)


@login_required
@user_passes_test(lambda u: u.is_staff)
def invitation_list(request):
    _repair_empty_invitation_codes()

    invitations = Invitation.objects.all().prefetch_related('projects').order_by('-created_at')
    if request.method == 'POST':
        form = InvitationForm(request.POST)
        if form.is_valid():
            invitation = form.save(commit=False)
            invitation.code = str(uuid.uuid4())
            invitation.created_by = request.user
            invitation.save()
            invitation.projects.set(form.cleaned_data['projects'])

            if invitation.email:
                register_url = f"{request.build_absolute_uri(reverse('register'))}?code={invitation.code}"
                expires_at = timezone.localtime(invitation.expires_at).strftime('%Y-%m-%d %H:%M %Z')
                invitation_projects = list(invitation.projects.all())
                if not invitation_projects and invitation.project:
                    invitation_projects = [invitation.project]
                email_context = {
                    'invitation': invitation,
                    'project_names': [project.name for project in invitation_projects],
                    'role_display': invitation.get_role_display(),
                    'expires_at': expires_at,
                    'register_url': register_url,
                    'signature': form.cleaned_data.get('signature') or 'The Yggdrasil team',
                }
                sender_email = form.cleaned_data.get('sender_email')

                try:
                    connection = get_connection(
                        username=settings.EMAIL_HOST_USER,
                        password=settings.EMAIL_HOST_PASSWORD,
                        fail_silently=False,
                    )
                    _send_invitation_email(invitation, email_context, sender_email, connection)
                    invitation.email_sent_at = timezone.now()
                    invitation.email_send_error = ''
                    invitation.save(update_fields=['email_sent_at', 'email_send_error'])
                    messages.success(request, f'Invitation created and email sent to {invitation.email}.')
                except Exception as exc:
                    invitation.email_send_error = str(exc)
                    invitation.save(update_fields=['email_send_error'])
                    logger.error('Failed to send invitation email for invitation %s', invitation.code, exc_info=True)
                    messages.warning(
                        request,
                        'Invitation created, but email delivery failed. Please send the invitation link manually.'
                    )
            else:
                messages.success(
                    request,
                    'Invitation created successfully. No email was sent because no recipient email was provided.'
                )

            return redirect('invitation_list')
    else:
        form = InvitationForm()
    users = User.objects.order_by('username').prefetch_related(Prefetch(
        'project_access',
        queryset=ProjectAccess.objects.select_related('project').order_by('project__name'),
    ))
    return render(request, 'registration/invitation_list.html', {
        'invitations': invitations,
        'users': users,
        'contact_emails': ', '.join(u.email for u in users if u.is_active and u.email),
        'form': form,
        'registration_base_url': request.build_absolute_uri(reverse('register'))
    })


@login_required
@user_passes_test(lambda u: u.is_staff)
@require_POST
def delete_invitation(request, code):
    invitation = get_object_or_404(Invitation, code=code)
    if not invitation.used_at:  # Only allow deleting unused invitations
        invitation.delete()
        messages.success(request, 'Invitation deleted successfully!')
    return redirect('invitation_list')
