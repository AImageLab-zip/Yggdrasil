"""A project may only be given what its own domain has.

The reported bug: the Django admin offered *every* modality, annotation method
and processing step on *every* project, so bite classification could be enabled
on a brain or laparoscopy project. ``AnnotationMethod.domain`` had existed since
migration 0043 and nothing read it; ``Modality`` had no domain at all.

These tests go through the admin's own ``formfield_for_manytomany``, which is
what both renders the widget and validates the POST, rather than through a
hand-built queryset -- the point of the fix is that one filter does both.
"""

from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory, TestCase

from brain.admin import BrainProjectAdmin
from brain.models import BrainProject
from common.models import AnnotationMethod, Modality, ProcessingStep, Project
from laparoscopy.admin import LaparoscopyProjectAdmin
from laparoscopy.models import LaparoscopyProject
from maxillo.admin import MaxilloProjectAdmin
from maxillo.models import MaxilloProject


class DomainScopedProjectAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cbct = Modality.objects.create(name="CBCT t", slug="cbct-t", domain="maxillo")
        cls.flair = Modality.objects.create(
            name="FLAIR t", slug="flair-t", domain="brain"
        )
        cls.video = Modality.objects.create(
            name="Video t", slug="video-t", domain="laparoscopy"
        )
        # Blank domain: available everywhere, the AnnotationMethod convention.
        cls.audio = Modality.objects.create(name="Audio t", slug="audio-t", domain="")

        cls.bite = AnnotationMethod.objects.create(
            name="Bite classification t", slug="bite-t", domain="maxillo"
        )
        cls.regions = AnnotationMethod.objects.create(
            name="Video regions t", slug="regions-t", domain="laparoscopy"
        )
        cls.captions = AnnotationMethod.objects.create(
            name="Voice captions t", slug="captions-t", domain=""
        )

        cls.cbct_step = ProcessingStep.objects.create(
            modality=cls.cbct, name="CBCT step", slug="cbct-step-t"
        )
        cls.flair_step = ProcessingStep.objects.create(
            modality=cls.flair, name="FLAIR step", slug="flair-step-t"
        )
        cls.audio_step = ProcessingStep.objects.create(
            modality=cls.audio, name="Audio step", slug="audio-step-t"
        )

    #: Only the rows these tests create. The migrations seed real modalities and
    #: steps (correctly domained, which is the backfill doing its job), and
    #: asserting over the whole table would make this a test of the seed data.
    OURS = {"cbct-t", "flair-t", "video-t", "audio-t"}

    def _choices(self, admin_class, model, field_name):
        """The queryset the admin would render *and validate against*."""
        admin = admin_class(model, AdminSite())
        request = RequestFactory().get("/")
        field = admin.formfield_for_manytomany(
            model._meta.get_field(field_name), request
        )
        return set(field.queryset)

    def _ours(self, admin_class, model, field_name):
        offered = self._choices(admin_class, model, field_name)
        if field_name == "disabled_steps":
            return {step for step in offered if step.modality.slug in self.OURS}
        return {row for row in offered if row.slug in self.OURS}

    def test_modalities_are_the_domain_plus_the_domain_blank_ones(self):
        self.assertEqual(
            self._ours(BrainProjectAdmin, BrainProject, "modalities"),
            {self.flair, self.audio},
        )
        self.assertEqual(
            self._ours(MaxilloProjectAdmin, MaxilloProject, "modalities"),
            {self.cbct, self.audio},
        )
        self.assertEqual(
            self._ours(LaparoscopyProjectAdmin, LaparoscopyProject, "modalities"),
            {self.video, self.audio},
        )

    def test_bite_classification_is_not_offered_outside_maxillo(self):
        """The reported case, stated as its own test."""
        for admin_class, model in (
            (BrainProjectAdmin, BrainProject),
            (LaparoscopyProjectAdmin, LaparoscopyProject),
        ):
            with self.subTest(domain=admin_class.domain):
                offered = self._choices(admin_class, model, "annotation_methods")
                self.assertNotIn(self.bite, offered)
                # ...and the domain-blank method still is.
                self.assertIn(self.captions, offered)

        self.assertIn(
            self.bite,
            self._choices(MaxilloProjectAdmin, MaxilloProject, "annotation_methods"),
        )

    def test_processing_steps_take_their_domain_from_their_modality(self):
        """A ProcessingStep has no domain column; it is its modality's."""
        self.assertEqual(
            self._ours(BrainProjectAdmin, BrainProject, "disabled_steps"),
            {self.flair_step, self.audio_step},
        )
        self.assertEqual(
            self._ours(MaxilloProjectAdmin, MaxilloProject, "disabled_steps"),
            {self.cbct_step, self.audio_step},
        )

    def test_a_cross_domain_selection_is_refused_on_save_not_only_hidden(self):
        """Filtering the queryset is what makes the POST fail, not just the widget.

        A hidden option can be posted by hand; ``ModelMultipleChoiceField``
        validates against this very queryset, so the id is rejected.
        """
        admin = BrainProjectAdmin(BrainProject, AdminSite())
        request = RequestFactory().get("/")
        FormClass = admin.get_form(request)
        form = FormClass(
            data={
                "name": "A brain project",
                "slug": "a-brain-project",
                "is_active": "on",
                "annotation_methods": [str(self.bite.pk)],
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("annotation_methods", form.errors)

    def test_the_admin_still_forces_its_own_domain_on_save(self):
        admin = BrainProjectAdmin(BrainProject, AdminSite())
        project = BrainProject(name="Forced", slug="forced", domain="maxillo")
        admin.save_model(RequestFactory().get("/"), project, None, change=False)
        self.assertEqual(Project.objects.get(slug="forced").domain, "brain")
