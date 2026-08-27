"""Tooth segmentation through the annotations app.

The interesting cases are the ones where segmentation differs from a measurement, because
everything they share is already covered by `tests_multi_target.py` and is reused rather
than reimplemented:

- **Labels are required.** An FDI code decides which segment a polygon is exported under,
  so an unknown code must be a refusal and not an unlabelled row that looks fine.
- **Its own set kind**, so "this patient's measurements" does not come to mean two things
  and `annotations_crosscheck` can still find both representations of one study.
- **No scratch payload**, because a tooth polygon *is* a list of points and a second copy
  allowed to go stale would only ever disagree with the items.

And one that is not a difference at all but matters more here: the live path and
`annotations_convert_legacy` must produce the *same rows*, or the cross-check reports drift
on every study anybody has edited and buries the signal it exists to give.
"""

import json
import uuid

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from annotations.adapters import legacy_maxillo
from annotations.adapters.tooth_segmentation import (
    MAX_POINTS_PER_POLYGON,
    SEGMENTATION_KIND,
    teeth_from_items,
    tooth_polygons,
)
from annotations.constants import PayloadFormat
from annotations.models import AnnotationSet, Geometry2DItem
from common.models import AnnotationMethod, FileRegistry, Project, ProjectAccess
from maxillo.models import Folder, Patient

SQUARE = [[10, 10], [30, 10], [30, 30], [10, 30]]
TRIANGLE = [[40, 40], [60, 40], [60, 60]]


class ToothPolygonAdapterTests(SimpleTestCase):
    def test_one_polygon_becomes_one_closed_image_pixel_polygon(self):
        [descriptor] = tooth_polygons({"36": [SQUARE]})
        self.assertEqual(descriptor["geometry_type"], "polygon")
        self.assertEqual(descriptor["coordinate_system"], "image_pixel")
        self.assertTrue(descriptor["closed"], "a polygon is closed by definition")
        self.assertEqual(descriptor["label_code"], "36")
        self.assertEqual(descriptor["attributes"], {"fdi": "36", "polygon_index": 0})

    def test_a_tooth_with_two_disjoint_polygons_becomes_two_items(self):
        # A molar split by a restoration, or a crown visible either side of an
        # obstruction. Merging them into one ring would invent geometry.
        descriptors = tooth_polygons({"36": [SQUARE, TRIANGLE]})
        self.assertEqual(len(descriptors), 2)
        self.assertEqual([d["order"] for d in descriptors], [0, 1])

    def test_the_output_is_ordered_so_two_conversions_agree_byte_for_byte(self):
        forwards = tooth_polygons({"11": [SQUARE], "36": [TRIANGLE]})
        backwards = tooth_polygons({"36": [TRIANGLE], "11": [SQUARE]})
        self.assertEqual(forwards, backwards)

    def test_coordinates_are_not_normalised(self):
        # The photograph is the resource, so rescaling here would make the stored form
        # differ from what the user drew for no reason a cross-check could explain.
        [descriptor] = tooth_polygons({"36": [SQUARE]})
        self.assertEqual(descriptor["points"], SQUARE)

    def test_a_malformed_map_is_refused(self):
        for teeth in ([], "nope", {"36": "nope"}, {"36": ["nope"]}):
            with self.assertRaises(ValidationError):
                tooth_polygons(teeth)

    def test_an_absurd_polygon_is_refused_rather_than_written(self):
        # A client bug that appends to a polygon in a loop would otherwise turn a stuck
        # mouse into an unbounded write.
        with self.assertRaisesMessage(ValidationError, "points, more than"):
            tooth_polygons({"36": [[[0, 0]] * (MAX_POINTS_PER_POLYGON + 1)]})
        with self.assertRaisesMessage(ValidationError, "polygons, more than"):
            tooth_polygons({"36": [SQUARE] * 33})

    def test_the_legacy_converter_and_the_live_path_are_the_same_function(self):
        """The property the cross-check depends on.

        Two implementations would drift, and the drift would surface as
        `annotations_crosscheck` reporting differences on every study anybody had edited.
        """
        teeth = {"11": [SQUARE], "36": [SQUARE, TRIANGLE]}
        self.assertEqual(legacy_maxillo.intraoral_segmentation(teeth), tooth_polygons(teeth))


class TeethFromItemsTests(SimpleTestCase):
    class _Item:
        def __init__(self, points, order, fdi, label_code=None):
            self.points = points
            self.order = order
            self.attributes = {"fdi": fdi, "polygon_index": order}
            self.label = type("L", (), {"code": label_code})() if label_code else None

    def test_items_rebuild_the_map_in_polygon_order(self):
        teeth = teeth_from_items(
            [
                self._Item(TRIANGLE, 1, "36", "36"),
                self._Item(SQUARE, 0, "36", "36"),
            ]
        )
        self.assertEqual(teeth, {"36": [SQUARE, TRIANGLE]})

    def test_the_label_wins_over_the_attribute(self):
        # The attribute duplicates the label deliberately, but the label is the row the
        # export reads, so a disagreement resolves to it.
        teeth = teeth_from_items([self._Item(SQUARE, 0, "11", "36")])
        self.assertEqual(list(teeth), ["36"])

    def test_a_row_with_neither_is_dropped_rather_than_filed_under_nothing(self):
        self.assertEqual(teeth_from_items([self._Item(SQUARE, 0, "")]), {})


class ToothSegmentationApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        suffix = uuid.uuid4().hex[:8]
        cls.project = Project.objects.create(
            name=f"seg-{suffix}", slug=f"seg-{suffix}", domain="maxillo"
        )
        cls.folder = Folder.objects.create(name="mx", project=cls.project)
        cls.patient = Patient.objects.create(
            patient_id=9601, folder=cls.folder, project=cls.project
        )
        cls.other = Patient.objects.create(
            patient_id=9602, folder=cls.folder, project=cls.project
        )
        cls.photo_a = cls._file(cls.patient, "a.jpg", "a")
        cls.photo_b = cls._file(cls.patient, "b.jpg", "b")
        cls.foreign = cls._file(cls.other, "x.jpg", "x")
        # The registry is seeded by `common.0043`; a project's own set is not, so a test
        # project has every method *off* until it says otherwise.
        cls.project.annotation_methods.set(
            AnnotationMethod.objects.filter(slug="intraoral_segmentation")
        )

    @classmethod
    def _file(cls, patient, name, hash_char):
        return FileRegistry.objects.create(
            patient=patient,
            file_type="intraoral_raw",
            file_path=f"maxillo/intraoral_raw/{name}",
            file_size=1,
            file_hash=hash_char * 64,
            domain="maxillo",
        )

    def setUp(self):
        self.user = User.objects.create_user(
            username=f"u{uuid.uuid4().hex[:8]}", password="pw"  # noqa: S106
        )
        ProjectAccess.objects.create(user=self.user, project=self.project, role="annotator")
        self.client.force_login(self.user)
        self.url = reverse(
            "maxillo:api_save_tooth_segmentation",
            kwargs={"patient_id": self.patient.patient_id},
        )
        self.state_url = reverse(
            "maxillo:api_tooth_segmentation_state",
            kwargs={"patient_id": self.patient.patient_id},
        )

    def _save(self, images, **extra):
        return self.client.post(
            self.url,
            data=json.dumps({"images": images, **extra}),
            content_type="application/json",
        )

    def _set(self):
        return AnnotationSet.objects.get(patient=self.patient, kind=SEGMENTATION_KIND)

    def _latest(self):
        return self._set().revisions.order_by("-revision_number").first()

    # -- the happy path -----------------------------------------------------

    def test_polygons_become_labelled_2d_items_under_their_own_set_kind(self):
        response = self._save([{"fileId": self.photo_a.id, "teeth": {"36": [SQUARE]}}])
        self.assertEqual(response.status_code, 200, response.content)

        annotation_set = self._set()
        self.assertEqual(annotation_set.kind, SEGMENTATION_KIND)
        self.assertEqual(annotation_set.label_schema.slug, "fdi-permanent")
        self.assertTrue(annotation_set.ever_annotated, "human work freezes the raw data")

        item = Geometry2DItem.objects.get(revision__annotation_set=annotation_set)
        self.assertEqual(item.geometry_type, "polygon")
        self.assertEqual(item.coordinate_system, "image_pixel")
        self.assertTrue(item.closed)
        self.assertEqual(item.label.code, "36")
        # Read from the seeded schema, not asserted from a table in this file: the value
        # is frozen by UniqueConstraint(schema, value) and the migration is its source.
        self.assertEqual(item.label.value, 22, "36 is quadrant 3, tooth 6 -> (3-1)*8+6 = 22")

    def test_the_measurements_set_is_untouched(self):
        # Filing polygons as measurements would make "this patient's measurements" mean
        # two different things, and would put them in one replace-the-set revision.
        self._save([{"fileId": self.photo_a.id, "teeth": {"36": [SQUARE]}}])
        self.assertFalse(
            AnnotationSet.objects.filter(patient=self.patient, kind="measurements").exists()
        )

    def test_no_scratch_payload_is_written(self):
        # A tooth polygon *is* a list of points; the items are the whole truth, and a
        # second copy allowed to go stale would only ever disagree with them.
        self._save([{"fileId": self.photo_a.id, "teeth": {"36": [SQUARE]}}])
        self.assertFalse(
            self._latest().payloads.filter(format=PayloadFormat.CORNERSTONE_STATE).exists()
        )

    def test_the_state_endpoint_returns_what_was_saved(self):
        self._save([{"fileId": self.photo_a.id, "teeth": {"36": [SQUARE, TRIANGLE]}}])
        state = self.client.get(self.state_url).json()
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["images"][str(self.photo_a.id)], {"36": [SQUARE, TRIANGLE]})

    def test_a_patient_with_no_work_gets_an_empty_state_not_a_404(self):
        state = self.client.get(self.state_url).json()
        self.assertEqual(state, {"revision": 0, "setId": None, "images": {}})

    # -- the multi-image behaviour, inherited from the shared writer ---------

    def test_saving_one_image_carries_the_others_polygons_forward(self):
        self._save(
            [
                {"fileId": self.photo_a.id, "teeth": {"36": [SQUARE]}},
                {"fileId": self.photo_b.id, "teeth": {"11": [TRIANGLE]}},
            ]
        )
        self._save(
            [{"fileId": self.photo_a.id, "teeth": {"36": [TRIANGLE]}}], expectedRevision=1
        )

        state = self.client.get(self.state_url).json()
        self.assertEqual(state["images"][str(self.photo_a.id)], {"36": [TRIANGLE]})
        self.assertEqual(
            state["images"][str(self.photo_b.id)], {"11": [TRIANGLE]},
            "the image the save did not name keeps its work",
        )

    def test_an_empty_teeth_map_is_how_a_tooth_is_cleared(self):
        self._save(
            [
                {"fileId": self.photo_a.id, "teeth": {"36": [SQUARE]}},
                {"fileId": self.photo_b.id, "teeth": {"11": [TRIANGLE]}},
            ]
        )
        self._save([{"fileId": self.photo_a.id, "teeth": {}}], expectedRevision=1)

        state = self.client.get(self.state_url).json()
        self.assertEqual(state["images"].get(str(self.photo_a.id), {}), {})
        self.assertEqual(state["images"][str(self.photo_b.id)], {"11": [TRIANGLE]})

    def test_a_stale_revision_is_a_409(self):
        self._save([{"fileId": self.photo_a.id, "teeth": {"36": [SQUARE]}}])
        response = self._save(
            [{"fileId": self.photo_a.id, "teeth": {"11": [SQUARE]}}], expectedRevision=0
        )
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()["conflict"])

    def test_the_first_image_claims_the_primary_slot_and_keeps_it(self):
        """This kind has its own set, so nothing competes for the slot.

        Unlike a photo joining a patient's *measurements* -- where a volume may already
        hold it -- there is no other modality here, so leaving it unclaimed would give the
        set no answer at all to "what is this mostly about". It must not ping-pong to
        whatever was saved last either.
        """
        self._save(
            [
                {"fileId": self.photo_a.id, "teeth": {"36": [SQUARE]}},
                {"fileId": self.photo_b.id, "teeth": {"11": [SQUARE]}},
            ]
        )
        self._save([{"fileId": self.photo_b.id, "teeth": {"11": [TRIANGLE]}}], expectedRevision=1)

        primary = self._set().targets.get(primary_slot=1)
        self.assertEqual(primary.source_resource.file_id, self.photo_a.id)

    # -- refusals -----------------------------------------------------------

    def test_an_unknown_fdi_code_is_refused_and_writes_nothing(self):
        """The load-bearing one.

        A polygon written unlabelled is exported under the wrong segment number and looks
        fine doing it, so `apply_descriptors` refuses rather than defaults. Deciduous codes
        are in this class deliberately: they would need their own schema.
        """
        for code in ("99", "51", "09", "abc"):
            with self.subTest(code=code):
                response = self._save([{"fileId": self.photo_a.id, "teeth": {code: [SQUARE]}}])
                self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(
            AnnotationSet.objects.filter(patient=self.patient, kind=SEGMENTATION_KIND).exists(),
            "nothing may be written when a label cannot be resolved",
        )

    def test_one_bad_tooth_aborts_the_whole_save(self):
        # The translation runs before the first row, so a partial write is impossible --
        # which matters here more than for measurements, because a partial write would
        # also read as a deletion on the teeth that never got written.
        response = self._save(
            [{"fileId": self.photo_a.id, "teeth": {"36": [SQUARE], "99": [TRIANGLE]}}]
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Geometry2DItem.objects.filter(target__isnull=False).exists())

    def test_another_patients_file_is_a_403_with_nothing_written(self):
        response = self._save(
            [
                {"fileId": self.photo_a.id, "teeth": {"36": [SQUARE]}},
                {"fileId": self.foreign.id, "teeth": {"11": [SQUARE]}},
            ]
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            AnnotationSet.objects.filter(patient=self.patient, kind=SEGMENTATION_KIND).exists()
        )

    def test_naming_one_file_twice_is_refused(self):
        response = self._save(
            [
                {"fileId": self.photo_a.id, "teeth": {"36": [SQUARE]}},
                {"fileId": self.photo_a.id, "teeth": {"11": [SQUARE]}},
            ]
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("already in this save", response.json()["error"])

    def test_a_malformed_body_is_a_400(self):
        for body in ({"images": []}, {"images": "nope"}, {}):
            response = self.client.post(
                self.url, data=json.dumps(body), content_type="application/json"
            )
            self.assertEqual(response.status_code, 400, body)
        response = self._save([{"fileId": self.photo_a.id, "teeth": "nope"}])
        self.assertEqual(response.status_code, 400)

    def test_a_reader_may_not_save_but_may_read(self):
        reader = User.objects.create_user(
            username=f"r{uuid.uuid4().hex[:8]}", password="pw"  # noqa: S106
        )
        ProjectAccess.objects.create(user=reader, project=self.project, role="viewer")
        self.client.force_login(reader)
        self.assertEqual(
            self._save([{"fileId": self.photo_a.id, "teeth": {"36": [SQUARE]}}]).status_code,
            403,
        )
        self.assertEqual(self.client.get(self.state_url).status_code, 200)

    def test_get_is_not_allowed_on_the_save_endpoint(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_a_project_with_the_method_off_refuses_and_writes_nothing(self):
        """The gate is a code path, not a convention.

        Finding F11 is the record of what happens otherwise: `update_classification` was
        the annotation endpoint that never asked, and it took a migration and an audit to
        find. Here the gate is inside `get_or_create_set`, so this endpoint could not skip
        it even by forgetting to.
        """
        self.project.annotation_methods.set([])
        response = self._save([{"fileId": self.photo_a.id, "teeth": {"36": [SQUARE]}}])
        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn("disabled for this project", response.json()["error"])
        self.assertFalse(
            AnnotationSet.objects.filter(patient=self.patient, kind=SEGMENTATION_KIND).exists()
        )
