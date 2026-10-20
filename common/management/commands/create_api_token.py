"""Issue a personal API token and print it once.

    python manage.py create_api_token alice --name "nightly upload" [--days 90]

The token acts as ``alice`` on ``/api/`` (see :mod:`common.api_tokens`). Only its
hash is stored: the printed value is the only copy.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from common.api_tokens import issue_token


class Command(BaseCommand):
    help = "Issue a personal API token for a user and print it (shown once)."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--name", required=True, help="What the token is for.")
        parser.add_argument(
            "--days", type=int, default=None,
            help="Expire the token after this many days (default: never).",
        )

    def handle(self, *args, **options):
        User = get_user_model()
        try:
            user = User.objects.get(username=options["username"])
        except User.DoesNotExist:
            raise CommandError(f"No user named {options['username']!r}")
        expires_at = None
        if options["days"] is not None:
            if options["days"] < 1:
                raise CommandError("--days must be at least 1")
            expires_at = timezone.now() + timedelta(days=options["days"])
        try:
            _token, key = issue_token(user, options["name"], expires_at=expires_at)
        except ValueError as exc:
            raise CommandError(str(exc))
        self.stderr.write("Token issued. It is shown once; store it now.")
        self.stdout.write(key)
