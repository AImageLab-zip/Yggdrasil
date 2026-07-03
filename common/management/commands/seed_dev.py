from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from common.models import Project, ProjectAccess


class Command(BaseCommand):
    help = (
        "Seed a local development database: projects, modalities, a superuser "
        "and a demo folder/patient per domain. Refuses to run with DEBUG=False. "
        "Idempotent - safe to run repeatedly."
    )

    def add_arguments(self, parser):
        parser.add_argument("--admin-username", default="admin")
        parser.add_argument(
            "--admin-password",
            default="admin",
            help="Only applied when the user is created; never overwrites an existing password.",
        )
        parser.add_argument("--skip-superuser", action="store_true")

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "seed_dev refuses to run with DEBUG=False - it is for local development only."
            )

        self.stdout.write("Seeding projects and modalities...")
        call_command("create_maxillo_modalities")
        call_command("setup_brain_modalities")
        call_command("setup_laparoscopy_modalities")

        admin_user = None
        if not options["skip_superuser"]:
            admin_user = self._ensure_superuser(
                options["admin_username"], options["admin_password"]
            )

        self._seed_maxillo(admin_user)
        self._seed_brain(admin_user)
        self._seed_laparoscopy(admin_user)

        self.stdout.write(self.style.SUCCESS("Dev seed complete."))
        if admin_user is not None:
            self.stdout.write(
                f"Login: {options['admin_username']} / {options['admin_password']}"
            )

    def _ensure_superuser(self, username, password):
        user, created = User.objects.get_or_create(
            username=username,
            defaults={
                "is_staff": True,
                "is_superuser": True,
                "email": "admin@example.invalid",
            },
        )
        if created:
            user.set_password(password)
            user.save()
            self.stdout.write(f"Created superuser '{username}'.")
        else:
            self.stdout.write(f"Superuser '{username}' already exists - password untouched.")

        # The ActiveProfileMiddleware requires a ProjectAccess row to enter an
        # app section, superuser or not.
        for project in Project.objects.filter(is_active=True):
            ProjectAccess.objects.get_or_create(
                user=user, project=project, defaults={"role": "admin"}
            )
        return user

    def _seed_maxillo(self, admin_user):
        from maxillo.models import Folder, Patient

        folder, _ = Folder.objects.get_or_create(
            name="Demo", parent=None, defaults={"created_by": admin_user}
        )
        patient, created = Patient.objects.get_or_create(
            name="Demo Patient", folder=folder
        )
        if created:
            self.stdout.write(f"Created maxillo demo patient {patient.patient_id}.")

    def _seed_brain(self, admin_user):
        from brain.models import Folder, Patient

        folder, _ = Folder.objects.get_or_create(
            name="Demo", parent=None, defaults={"created_by": admin_user}
        )
        patient, created = Patient.objects.get_or_create(name="Demo Patient")
        patient.folders.add(folder)
        if created:
            self.stdout.write(f"Created brain demo patient {patient.patient_id}.")

    def _seed_laparoscopy(self, admin_user):
        from laparoscopy.models import Folder, Patient

        folder, _ = Folder.objects.get_or_create(
            name="Demo", parent=None, defaults={"created_by": admin_user}
        )
        patient, created = Patient.objects.get_or_create(
            name="Demo Patient", folder=folder
        )
        if created:
            self.stdout.write(f"Created laparoscopy demo patient {patient.patient_id}.")
