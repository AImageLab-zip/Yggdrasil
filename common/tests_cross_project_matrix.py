"""Cross-project authorization matrix, generated from the URL resolver.

For every domain, an admin of project A requests every URL that names an object
(patient, file, caption, folder, export, job, project) with that object taken
from project B of the same domain. No response may be a success, and nothing
about B's objects may change.

The URL list is not hand-written: a URL added tomorrow is covered the day it is
routed. That is the point -- this bug class (a check answered against the
session's project, or a check missing altogether) was fixed screen by screen
three times, and each time the screens nobody listed kept it.
"""

from unittest import mock

from django.apps import apps
from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import URLPattern, URLResolver, get_resolver

from common.domains import DOMAINS, fk_fields_for
from common.models import FileRegistry, Job, Project, ProjectAccess

#: URL kwargs that name an object, and the fixture attribute that fills them.
OBJECT_KWARGS = {
    "patient_id": "patient_pk",
    "file_id": "file_pk",
    "caption_id": "caption_pk",
    "folder_id": "folder_pk",
    "export_id": "export_pk",
    "job_id": "job_pk",
    "project_id": "project_pk",
    "project_slug": "project_slug",
}

#: Filler for the remaining kwargs of a URL that names an object.
OTHER_KWARGS = {
    "filename": "x.bin",
    "bundle_key": "x",
    "modality_slug": "cbct",
    "level": 0,
    "col": 0,
    "row": 0,
    "pk": 1,
}

#: Prefixes that are not object-level app routes: the Django admin (staff-only,
#: and its own permission system) and the runner API (bearer-token auth).
SKIPPED_PREFIXES = ("admin/", "api/runner/")


def _walk(resolver, prefix=""):
    for entry in resolver.url_patterns:
        pattern = prefix + str(entry.pattern)
        if isinstance(entry, URLResolver):
            yield from _walk(entry, pattern)
        elif isinstance(entry, URLPattern):
            yield pattern, entry


def object_routes():
    """``(route, converters)`` for every routed URL that names an object."""
    routes = []
    for route, entry in _walk(get_resolver()):
        if route.startswith(SKIPPED_PREFIXES):
            continue
        names = set(entry.pattern.converters)
        if names & set(OBJECT_KWARGS) and names <= set(OBJECT_KWARGS) | set(OTHER_KWARGS):
            routes.append((route, entry.pattern.converters))
    return routes


def _fill(route, converters, values):
    url = "/" + route.lstrip("^").rstrip("$")
    for name, converter in converters.items():
        value = values[name]
        tag = {"IntConverter": "int", "SlugConverter": "slug", "PathConverter": "path"}.get(
            type(converter).__name__, "str"
        )
        url = url.replace(f"<{tag}:{name}>", str(value)).replace(f"<{name}>", str(value))
    return url


class _TwoProjectsPerDomain(TestCase):
    """Projects A and B in every domain; ``admin_a`` administers A only."""

    def setUp(self):
        patcher = mock.patch("common.signals.celery_app.send_task")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.admin_a = User.objects.create_user("matrix-admin-a", password="x")
        self.owner_b = User.objects.create_user("matrix-owner-b", password="x")
        self.fixtures = {domain: self._domain_fixture(domain) for domain in sorted(DOMAINS)}

    def _domain_fixture(self, domain):
        project_a = Project.objects.create(name=f"A {domain}", slug=f"matrix-a-{domain}", domain=domain)
        project_b = Project.objects.create(name=f"B {domain}", slug=f"matrix-b-{domain}", domain=domain)
        ProjectAccess.objects.create(user=self.admin_a, project=project_a, role="admin")
        ProjectAccess.objects.create(user=self.owner_b, project=project_b, role="admin")

        Folder = apps.get_model(domain, "Folder")
        Patient = apps.get_model(domain, "Patient")
        VoiceCaption = apps.get_model(domain, "VoiceCaption")
        Export = apps.get_model(domain, "Export")
        patient_fk = fk_fields_for(domain)[0]

        folder = Folder.objects.create(name="B folder", project=project_b)
        patient = Patient.objects.create(name="B patient", project=project_b, folder=folder)
        file_obj = FileRegistry.objects.create(
            file_type="generic_raw", file_path=f"matrix/{domain}/raw.bin", file_size=1,
            file_hash="0" * 64, domain=domain, **{patient_fk: patient},
        )
        caption = VoiceCaption.objects.create(
            patient=patient, user=self.owner_b, duration=1.0, text_caption="matrix caption text"
        )
        export = Export.objects.create(user=self.owner_b, query_params={}, status="completed")
        job = Job.objects.create(
            domain=domain, modality_slug="matrix", status="completed", **{patient_fk: patient}
        )
        return {
            "domain": domain,
            "project_pk": project_b.pk,
            "project_slug": project_b.slug,
            "folder_pk": folder.pk,
            "patient_pk": patient.pk,
            "file_pk": file_obj.pk,
            "caption_pk": caption.pk,
            "export_pk": export.pk,
            "job_pk": job.pk,
            "models": (Patient, Folder, VoiceCaption, Export),
            "patient_fk": patient_fk,
        }

    def _snapshot(self, fx):
        Patient, Folder, VoiceCaption, Export = fx["models"]
        patient = Patient.objects.filter(pk=fx["patient_pk"]).values(
            "name", "project_id", "folder_id", "deleted"
        ).first() if any(f.name == "deleted" for f in Patient._meta.fields) else (
            Patient.objects.filter(pk=fx["patient_pk"]).values("name", "project_id", "folder_id").first()
        )
        caption = VoiceCaption.objects.filter(pk=fx["caption_pk"]).values("text_caption").first()
        export = Export.objects.filter(pk=fx["export_pk"]).values(
            "share_mode", "share_token", "expires_at"
        ).first()
        return {
            "patient": patient,
            "folder": Folder.objects.filter(pk=fx["folder_pk"]).values("name", "project_id").first(),
            "caption": caption,
            "export": export,
            "file": FileRegistry.objects.filter(pk=fx["file_pk"]).values("file_path").first(),
            "jobs": sorted(
                Job.objects.filter(**{fx["patient_fk"]: fx["patient_pk"]}).values_list("pk", "status")
            ),
        }


class CrossProjectMatrixTests(_TwoProjectsPerDomain):
    def test_an_admin_of_one_project_reaches_nothing_in_another(self):
        routes = object_routes()
        self.assertGreater(len(routes), 50, "the resolver walk found suspiciously few routes")
        client = Client(raise_request_exception=False)
        client.force_login(self.admin_a)

        for domain, fx in self.fixtures.items():
            values = {**OTHER_KWARGS, **{k: fx[v] for k, v in OBJECT_KWARGS.items()}}
            prefix = f"{domain}/"
            for route, converters in routes:
                # Domain routes are exercised with their own domain's objects; the
                # shared /api/ routes with every domain's.
                if "/" in route.split("<", 1)[0] and not route.startswith((prefix, "api/")):
                    continue
                url = _fill(route, converters, values)
                for method in ("get", "post", "delete"):
                    before = self._snapshot(fx)
                    session = client.session
                    # The session points at project A -- the context a
                    # session-scoped check would wrongly authorize against.
                    session["current_project_id"] = Project.objects.get(
                        slug=f"matrix-a-{domain}"
                    ).pk
                    session.save()
                    if method == "get":
                        response = client.get(url)
                    else:
                        response = getattr(client, method)(
                            url, data="{}", content_type="application/json"
                        )
                    with self.subTest(domain=domain, method=method.upper(), url=url):
                        # Refused, and refused cleanly: a 5xx is a crash on the
                        # denial path, and it hid a broken view here once already.
                        self.assertTrue(
                            300 <= response.status_code < 500,
                            f"{method.upper()} {url} -> {response.status_code}",
                        )
                        self.assertEqual(self._snapshot(fx), before, f"{method.upper()} {url} mutated B")


class CrossProjectBodyWriteTests(_TwoProjectsPerDomain):
    """Writes the matrix cannot reach with an empty body: they name their targets
    in the body, or need a confirmation flag before they act."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin_a)
        for domain in DOMAINS:
            session = self.client.session
            session["current_project_id"] = Project.objects.get(slug=f"matrix-a-{domain}").pk
            session.save()

    def _post(self, url, body, method="post"):
        import json

        return getattr(self.client, method)(url, data=json.dumps(body), content_type="application/json")

    def test_confirmed_caption_delete_is_refused(self):
        from django.urls import reverse

        for domain, fx in self.fixtures.items():
            with self.subTest(domain=domain):
                url = reverse(f"{domain}:delete_voice_caption", args=[fx["patient_pk"], fx["caption_pk"]])
                before = self._snapshot(fx)
                response = self._post(url, {"admin_confirmed": True}, method="delete")
                self.assertIn(response.status_code, (403, 404))
                self.assertEqual(self._snapshot(fx), before)

    def test_bulk_actions_skip_patients_of_other_projects(self):
        from django.urls import NoReverseMatch, reverse

        bodies = {
            "bulk_delete_patients": lambda fx: {"scan_ids": [fx["patient_pk"]]},
            "bulk_purge_patients": lambda fx: {"scan_ids": [fx["patient_pk"]]},
            "bulk_rerun_processing": lambda fx: {"scan_ids": [fx["patient_pk"]], "jobs": ["matrix"]},
            "add_patients_to_folder": lambda fx: {"scan_ids": [fx["patient_pk"]], "folder_id": fx["folder_pk"]},
            "remove_patients_from_folder": lambda fx: {"scan_ids": [fx["patient_pk"]], "folder_id": fx["folder_pk"]},
        }
        for domain, fx in self.fixtures.items():
            for name, body in bodies.items():
                try:
                    url = reverse(f"{domain}:{name}")
                except NoReverseMatch:
                    continue
                with self.subTest(domain=domain, view=name):
                    before = self._snapshot(fx)
                    response = self._post(url, body(fx))
                    if response.status_code == 200:
                        self.assertNotIn(response.json().get("updated"), (1,))
                    self.assertEqual(self._snapshot(fx), before)

    def test_moving_own_patients_into_another_projects_folder_is_refused(self):
        from django.urls import NoReverseMatch, reverse

        for domain, fx in self.fixtures.items():
            try:
                url = reverse(f"{domain}:add_patients_to_folder")
            except NoReverseMatch:
                continue
            Patient, Folder, _, _ = fx["models"]
            project_a = Project.objects.get(slug=f"matrix-a-{domain}")
            own = Patient.objects.create(
                name="A patient", project=project_a,
                folder=Folder.objects.create(name="A folder", project=project_a),
            )
            with self.subTest(domain=domain):
                self._post(url, {"scan_ids": [own.pk], "folder_id": fx["folder_pk"]})
                own.refresh_from_db()
                self.assertEqual(own.project_id, project_a.pk)
