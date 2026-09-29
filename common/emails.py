"""Branded transactional email: a text body, an HTML alternative, the logo inline.

The invitation email (``maxillo/views/auth.py``) and the password-reset email
(``common/password_reset.py``) share this, so the two cannot drift apart. The
HTML templates reference the logo as ``cid:yggdrasil-logo``: tables and inline
styles only, no external images, because mail clients block those.
"""
import logging
from email.mime.image import MIMEImage

from django.contrib.staticfiles import finders
from django.core.mail import EmailMultiAlternatives

logger = logging.getLogger(__name__)

LOGO_CID = "yggdrasil-logo"


def send_branded_email(subject, text_body, html_body, from_email, to, connection=None):
    """Send ``text_body`` with ``html_body`` as its HTML alternative and the logo inline."""
    message = EmailMultiAlternatives(subject, text_body, from_email, to, connection=connection)
    message.attach_alternative(html_body, "text/html")

    logo_path = finders.find("icons/email-logo.png")
    if logo_path:
        # multipart/related keeps clients from listing the inline logo as an attachment.
        message.mixed_subtype = "related"
        with open(logo_path, "rb") as fh:
            logo = MIMEImage(fh.read(), "png")
        logo.add_header("Content-ID", f"<{LOGO_CID}>")
        logo.add_header("Content-Disposition", "inline", filename="yggdrasil-logo.png")
        message.attach(logo)
    else:
        logger.warning("Email logo not found; sending %r without it", subject)

    message.send(fail_silently=False)
