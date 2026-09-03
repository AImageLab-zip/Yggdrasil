"""Patient-list filter bar: only what the project actually collects."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from annotations.services.segmentation import save_tooth_segmentation
from common.models import (
    AnnotationMethod,
    FileRegistry,
    Modality,
    Project,
    ProjectAccess,
)
from maxillo.models import (
    Classification,
    Folder,
    Patient,
)


def _method(slug, name):
    method, _ = AnnotationMethod.objects.get_or_create(slug=slug, defaults={"name": name})
    method.is_active = True
    method.save(update_fields=["is_active"])
    return method


class PresenceFilterVisibilityTests(TestCase):
    def setUp(self):
        self.cbct, _ = Modality.objects.get_or_create(slug="cbct", defaults={"name": "CBCT"})
        self.ios, _ = Modality.objects.get_or_create(slug="ios", defaults={"name": "IOS"})
        self.photo, _ = Modality.objects.get_or_create(
            slug="intraoral-photo", defaults={"name": "Intraoral Photographs"}
        )
        self.captions = _method("voice_caption", "Voice Captions")
        self.bite = _method("bite_classification", "Bite Classification")
        self.landmarks = _method("ios_landmarks", "IOS Landmarks")
        self.segmentation = _method("intraoral_segmentation", "Intraoral Segmentation")

        self.user = User.objects.create_user(username="filters-admin", password="x")

    def _visit(self, project):
        ProjectAccess.objects.get_or_create(
            user=self.user, project=project, defaults={"role": "admin"}
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["current_project_id"] = project.id
        session.save()
        return self.client.get(reverse("maxillo:patient_list"))

    def test_a_cbct_only_project_offers_only_the_reports_filter(self):
        project = Project.objects.create(name="TF4 Filters", slug="tf4-filters", domain="maxillo")
        project.modalities.set([self.cbct])
        project.annotation_methods.set([self.captions])

        response = self._visit(project)

        keys = [spec["key"] for spec in response.context["presence_filter_specs"]]
        self.assertEqual(keys, ["reports"])
        self.assertNotContains(response, "presence_landmarks")
        self.assertNotContains(response, "presence_bite_classification")

    def test_a_full_project_offers_every_collected_annotation(self):
        project = Project.objects.create(name="Full Filters", slug="full-filters", domain="maxillo")
        project.modalities.set([self.cbct, self.ios, self.photo])
        project.annotation_methods.set(
            [self.captions, self.bite, self.landmarks, self.segmentation]
        )

        response = self._visit(project)

        keys = [spec["key"] for spec in response.context["presence_filter_specs"]]
        self.assertEqual(
            keys,
            [
                "reports",
                "presence_bite_classification",
                "presence_landmarks",
                "presence_segmentation",
            ],
        )

    def test_landmarks_need_the_ios_modality_not_just_the_method(self):
        project = Project.objects.create(name="No IOS", slug="no-ios-filters", domain="maxillo")
        project.modalities.set([self.cbct])
        project.annotation_methods.set([self.captions, self.landmarks])

        response = self._visit(project)

        keys = [spec["key"] for spec in response.context["presence_filter_specs"]]
        self.assertEqual(keys, ["reports"])

    def test_a_project_collecting_no_annotations_offers_no_presence_filters(self):
        project = Project.objects.create(name="Bare", slug="bare-filters", domain="maxillo")
        project.modalities.set([self.cbct])
        project.annotation_methods.set([])

        response = self._visit(project)

        self.assertEqual(response.context["presence_filter_specs"], [])

    def test_the_active_value_round_trips_into_the_rendered_button(self):
        project = Project.objects.create(name="Active", slug="active-filters", domain="maxillo")
        project.modalities.set([self.cbct])
        project.annotation_methods.set([self.captions])
        ProjectAccess.objects.get_or_create(
            user=self.user, project=project, defaults={"role": "admin"}
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["current_project_id"] = project.id
        session.save()

        response = self.client.get(reverse("maxillo:patient_list"), {"has_reports": "yes"})

        spec = response.context["presence_filter_specs"][0]
        self.assertEqual(spec["value"], "yes")
        self.assertContains(response, 'id="reportsFilterValue"')


class SegmentationPresenceFilterTests(TestCase):
    def setUp(self):
        self.photo, _ = Modality.objects.get_or_create(
            slug="intraoral-photo", defaults={"name": "Intraoral Photographs"}
        )
        self.project = Project.objects.create(name="Seg", slug="seg-filters", domain="maxillo")
        self.project.modalities.set([self.photo])
        self.project.annotation_methods.set([_method("intraoral_segmentation", "Intraoral Segmentation")])
        self.folder = Folder.objects.create(name="General", project=self.project)
        self.user = User.objects.create_user(username="seg-admin", password="x")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.client.force_login(self.user)
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()

    def test_tooth_segmentation_presence_filters_the_list(self):
        segmented = Patient.objects.create(name="Segmented", project=self.project, folder=self.folder)
        Patient.objects.create(name="Plain", project=self.project, folder=self.folder)
        image = FileRegistry.objects.create(
            patient=segmented,
            domain="maxillo",
            file_type="intraoral_raw",
            file_path="maxillo/tests/photo.png",
            file_size=4,
            file_hash="1" * 64,
        )
        # Through the service, because that is the only writer now: the filter reads
        # `annotations/`, and a row poked into the legacy table would no longer be found
        # -- which is the point of moving it.
        save_tooth_segmentation(
            segmented,
            images=[
                {
                    "file_obj": image,
                    "teeth": {"11": [[[1, 1], [9, 1], [9, 9], [1, 9]]]},
                }
            ],
        )

        response = self.client.get(reverse("maxillo:patient_list"), {"has_segmentation": "yes"})

        self.assertEqual(
            [item["patient"] for item in response.context["page_obj"].object_list], [segmented]
        )


class BrainPresenceFilterTests(TestCase):
    """The filter bar is shared, so brain must keep its own Reports filter."""

    def setUp(self):
        self.mri, _ = Modality.objects.get_or_create(
            slug="braintumor-mri-t1", defaults={"name": "Brain MRI T1"}
        )
        self.project = Project.objects.create(name="Brain Filters", slug="brain-filters", domain="brain")
        self.project.modalities.set([self.mri])
        self.project.annotation_methods.set([_method("voice_caption", "Voice Captions")])
        self.user = User.objects.create_user(username="brain-filters", password="x")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.client.force_login(self.user)
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()

    def test_brain_offers_the_reports_filter_and_no_maxillo_ones(self):
        response = self.client.get(reverse("brain:patient_list"))

        keys = [spec["key"] for spec in response.context["presence_filter_specs"]]
        self.assertEqual(keys, ["reports"])


class RetiredFilterContextTests(TestCase):
    """has_ios / has_cbct / has_voice were read and passed to the template but
    never applied to the queryset; they are gone."""

    def setUp(self):
        self.project = Project.objects.create(name="Retired", slug="retired-filters", domain="maxillo")
        self.user = User.objects.create_user(username="retired-admin", password="x")
        ProjectAccess.objects.create(user=self.user, project=self.project, role="admin")
        self.client.force_login(self.user)
        session = self.client.session
        session["current_project_id"] = self.project.id
        session.save()

    def test_dead_filter_context_keys_are_gone(self):
        response = self.client.get(reverse("maxillo:patient_list"))
        for key in ("has_ios_filter", "has_cbct_filter", "has_voice_filter",
                    "show_maxillo_presence_filters"):
            self.assertNotIn(key, response.context)
