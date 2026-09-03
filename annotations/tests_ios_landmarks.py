"""IOS dental landmarks through the annotations app.

The interesting cases are the ones where landmarks differ from tooth polygons, because
everything they share -- one revision spanning several resources, carrying forward the ones
a save did not name, the optimistic-concurrency check -- is already covered by
``tests_multi_target.py`` and reused rather than reimplemented.

What is specific here:

- **The coordinates are ``resource_local``**, one mesh's own object space, so the model
  refuses to write them without a resolved target resource. Naming the mesh is not a
  convention this module follows; it is a rule it cannot get around.
- **The mesh is resolved by the server**, never sent by the client, so a stale viewer
  cannot file points against geometry nobody was looking at.
- **The document round-trips.** The export renders the same JSON from the record that the
  legacy artifact held, so moving the storage did not change what a clinician downloads.
- **One conversion**, shared with ``annotations_materialize_landmarks``, or
  ``annotations_crosscheck`` reports drift on every study anybody has edited.
"""

import json
import uuid
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from annotations.adapters import legacy_maxillo
from annotations.adapters.ios_landmarks import (
    LANDMARKS_KIND,
    by_jaw,
    ios_landmarks,
    jaw_for_tooth,
    landmark_key,
    landmarks_from_items,
    normalize_worker_document,
)
from annotations.constants import AnnotationOrigin, CoordinateSystem
from annotations.models import AnnotationSet, SpatialAnnotation3DItem
from annotations.services.ios_landmarks import (
    MESH_ROLES,
    ios_landmarks_state,
    save_ios_landmarks,
)
from common.models import AnnotationMethod, FileRegistry, Project, ProjectAccess
from maxillo.models import Folder, Patient

#: The shared fixture, also read by `frontend/tests/landmarkDocument.test.js`, so the two
#: implementations of the document shape cannot drift apart unnoticed.
FIXTURE = json.loads(
    (Path(settings.BASE_DIR) / "common" / "fixtures" / "ios_landmarks_document.json").read_text()
)
DOCUMENT = FIXTURE["document"]
FIXTURE_PATIENT_ID = 7


def _mesh(patient, file_type, name, hash_char):
    return FileRegistry.objects.create(
        patient=patient,
        file_type=file_type,
        file_path=f"maxillo/ios/{name}",
        file_size=1,
        file_hash=hash_char * 64,
        domain="maxillo",
    )


def _by_jaw(document):
    """The fixture document as the wire format: FDI-keyed, per arch."""
    jaws = {"upper": {}, "lower": {}}
    for key, entry in document.items():
        _, jaw, _, tooth = key.split("_")
        jaws[jaw][tooth] = entry
    return jaws


class LandmarkAdapterTests(SimpleTestCase):
    def test_a_single_point_type_becomes_one_resource_local_point(self):
        descriptors = ios_landmarks(
            {"7_upper_FDI_11": {"incisal": [1.5, -2.25, 3.125]}}, patient_id=7
        )
        [descriptor] = descriptors
        self.assertEqual(descriptor["geometry_type"], "point")
        self.assertEqual(descriptor["coordinate_system"], CoordinateSystem.RESOURCE_LOCAL)
        self.assertEqual(descriptor["points"], [[1.5, -2.25, 3.125]])
        self.assertEqual(descriptor["label_code"], "11")
        self.assertEqual(descriptor["attributes"]["landmark"], "incisal")
        self.assertEqual(descriptor["attributes"]["jaw"], "upper")
        # No frame of reference: these coordinates have no patient frame at all, and
        # `validate_geometry_3d` refuses one alongside `resource_local`.
        self.assertEqual(descriptor["frame_of_reference_uid"], "")

    def test_cusps_become_one_item_per_point_and_not_a_polyline(self):
        # Cusps are unordered landmarks that happen to be stored in a list; a polyline
        # would assert an order and a connectivity the original never had.
        descriptors = ios_landmarks(
            {"7_upper_FDI_11": {"cusps": [[1, 1, 1], [2, 2, 2], [3, 3, 3]]}}, patient_id=7
        )
        self.assertEqual(len(descriptors), 3)
        self.assertTrue(all(d["geometry_type"] == "point" for d in descriptors))
        self.assertEqual([d["order"] for d in descriptors], [0, 1, 2])
        self.assertEqual([d["attributes"]["index"] for d in descriptors], [0, 1, 2])

    def test_a_base_plane_keeps_its_z_axis_verbatim(self):
        # The model stores a plane as three points and z is derivable -- but a recomputed
        # cross product of floats is not the number that was stored.
        [descriptor] = ios_landmarks(
            {
                "7_upper_FDI_11": {
                    "basePlane": {
                        "origin": [0, 0, 0],
                        "xAxis": [1, 0, 0],
                        "yAxis": [0, 1, 0],
                        "zAxis": [0, 0, 1],
                    }
                }
            },
            patient_id=7,
        )
        self.assertEqual(descriptor["geometry_type"], "plane")
        self.assertEqual(len(descriptor["points"]), 3)
        self.assertEqual(descriptor["attributes"]["zAxis"], [0.0, 0.0, 1.0])

    def test_another_patients_key_is_refused_rather_than_skipped(self):
        with self.assertRaises(ValidationError):
            ios_landmarks({"8_upper_FDI_11": {"incisal": [0, 0, 0]}}, patient_id=7)

    def test_a_tooth_in_the_wrong_jaw_is_refused(self):
        # Tooth 31 is a lower incisor. A key claiming it is upper is a document whose two
        # statements about the arch disagree, and one of them is going to be acted on.
        with self.assertRaises(ValidationError):
            ios_landmarks({"7_upper_FDI_31": {"incisal": [0, 0, 0]}}, patient_id=7)

    def test_a_boolean_is_not_a_coordinate(self):
        # `True` is an `int` in Python, so without an explicit check it would store as 1.0
        # and read back as a plausible number.
        with self.assertRaises(ValidationError):
            ios_landmarks({"7_upper_FDI_11": {"incisal": [True, 0, 0]}}, patient_id=7)

    def test_the_output_is_ordered_so_two_conversions_agree(self):
        forwards = ios_landmarks(DOCUMENT, patient_id=FIXTURE_PATIENT_ID)
        shuffled = {k: DOCUMENT[k] for k in reversed(list(DOCUMENT))}
        self.assertEqual(forwards, ios_landmarks(shuffled, patient_id=FIXTURE_PATIENT_ID))

    def test_the_legacy_adapter_delegates_to_this_one(self):
        # Decision #6 keeps the legacy artifact readable for one release as a cross-check,
        # and a cross-check only means something if both sides are the same conversion.
        self.assertEqual(
            legacy_maxillo.ios_landmarks(DOCUMENT, patient_id=FIXTURE_PATIENT_ID),
            ios_landmarks(DOCUMENT, patient_id=FIXTURE_PATIENT_ID),
        )

    def test_jaw_is_derived_from_the_fdi_quadrant(self):
        self.assertEqual([jaw_for_tooth(t) for t in ("11", "26")], ["upper", "upper"])
        self.assertEqual([jaw_for_tooth(t) for t in ("31", "48")], ["lower", "lower"])
        self.assertEqual(landmark_key(7, "31"), "7_lower_FDI_31")


class LandmarkServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        suffix = uuid.uuid4().hex[:8]
        cls.project = Project.objects.create(
            name=f"ios-{suffix}", slug=f"ios-{suffix}", domain="maxillo"
        )
        cls.folder = Folder.objects.create(name="mx", project=cls.project)
        cls.patient = Patient.objects.create(
            patient_id=FIXTURE_PATIENT_ID, folder=cls.folder, project=cls.project
        )
        cls.upper = _mesh(cls.patient, "ios_raw_upper", "upper.stl", "a")
        cls.lower = _mesh(cls.patient, "ios_raw_lower", "lower.stl", "b")
        cls.project.annotation_methods.set(
            AnnotationMethod.objects.filter(slug="ios_landmarks")
        )

    def _save(self, jaws=None, **kwargs):
        jaws = jaws if jaws is not None else _by_jaw(DOCUMENT)
        return save_ios_landmarks(
            self.patient,
            meshes=[
                {"file_obj": self.upper, "jaw": "upper", "landmarks": jaws["upper"]},
                {"file_obj": self.lower, "jaw": "lower", "landmarks": jaws["lower"]},
            ],
            **kwargs,
        )

    def _state(self):
        return ios_landmarks_state(self.patient, domain_field="patient")

    # ---------------------------------------------------------------- the round trip

    def test_the_document_survives_a_full_round_trip(self):
        """The proof the export did not change under the move.

        Semantic equality, not byte equality: the legacy file preserved the client's key
        insertion order and the renderer sorts. Claiming bytes would be claiming something
        the corpus cannot honour -- what matters is that every landmark, every coordinate
        and every plane comes back.
        """
        self._save()
        self.assertEqual(self._state()["document"], DOCUMENT)

    def test_every_coordinate_survives_exactly(self):
        # The one that would actually bite: a float that round-trips through JSONField as
        # 3.1249999 is a landmark that has moved, and semantic equality above is only safe
        # to rely on because of this.
        self._save()
        rendered = self._state()["document"]
        self.assertEqual(
            repr(rendered["7_upper_FDI_11"]["incisal"]), repr([1.5, -2.25, 3.125])
        )
        self.assertEqual(
            rendered["7_lower_FDI_48"]["distal"], [-10.5, 11.25, -12.0625]
        )

    def test_the_renderer_is_deterministic(self):
        # Two renders of one revision must be byte-identical, or a diffed export shows
        # churn nobody caused.
        self._save()
        first = json.dumps(self._state()["document"], separators=(",", ":"))
        second = json.dumps(self._state()["document"], separators=(",", ":"))
        self.assertEqual(first, second)

    def test_multi_point_types_keep_their_order(self):
        self._save()
        cusps = self._state()["document"]["7_upper_FDI_11"]["cusps"]
        self.assertEqual(cusps, [[1.1, -2.1, 3.1], [1.2, -2.2, 3.2], [1.3, -2.3, 3.3]])

    # ---------------------------------------------------------------- anchoring

    def test_each_jaw_gets_its_own_target_under_its_own_role(self):
        revision = self._save()
        roles = {
            target.role: target.source_resource.file_id
            for target in revision.annotation_set.targets.select_related("source_resource")
        }
        self.assertEqual(
            roles, {MESH_ROLES["upper"]: self.upper.pk, MESH_ROLES["lower"]: self.lower.pk}
        )

    def test_the_items_are_resource_local_with_no_frame_of_reference(self):
        self._save()
        items = SpatialAnnotation3DItem.objects.filter(
            revision__annotation_set__patient=self.patient
        )
        self.assertTrue(items.exists())
        for item in items:
            self.assertEqual(item.coordinate_system, CoordinateSystem.RESOURCE_LOCAL)
            self.assertEqual(item.frame_of_reference_uid, "")

    def test_a_tooth_sent_under_the_wrong_jaw_is_refused(self):
        jaws = {"upper": {"31": {"incisal": [0, 0, 0]}}, "lower": {}}
        with self.assertRaises(ValidationError):
            self._save(jaws=jaws)

    def test_an_unknown_fdi_code_is_refused_not_written_unlabelled(self):
        # A point written unlabelled is a point exported against the wrong tooth, looking
        # fine doing it.
        jaws = {"upper": {"99": {"incisal": [0, 0, 0]}}, "lower": {}}
        with self.assertRaises(ValidationError):
            self._save(jaws=jaws)
        self.assertFalse(SpatialAnnotation3DItem.objects.exists())

    # ---------------------------------------------------------------- revisions

    def test_a_jaw_the_save_did_not_name_is_carried_forward(self):
        self._save()
        before = self._state()
        save_ios_landmarks(
            self.patient,
            meshes=[{"file_obj": self.upper, "jaw": "upper", "landmarks": {}}],
            expected_revision=before["revision"],
        )
        after = self._state()
        self.assertEqual(after["jaws"]["upper"], {}, "the named jaw was cleared")
        self.assertEqual(
            after["jaws"]["lower"],
            before["jaws"]["lower"],
            "the jaw the save never mentioned kept its landmarks",
        )

    def test_an_empty_map_is_how_a_deletion_is_expressed(self):
        self._save()
        self._save(jaws={"upper": {}, "lower": {}}, expected_revision=self._state()["revision"])
        self.assertEqual(self._state()["document"], {})

    def test_a_stale_expected_revision_is_a_conflict(self):
        from annotations.services.exceptions import AnnotationConflict

        self._save()
        self._save(expected_revision=self._state()["revision"])
        with self.assertRaises(AnnotationConflict):
            self._save(expected_revision=1)

    def test_a_prediction_does_not_set_ever_annotated(self):
        # Model output is not human annotation work, and `ever_annotated` is what freezes
        # a patient's raw scans.
        self._save(origin=AnnotationOrigin.PREDICTION)
        annotation_set = AnnotationSet.objects.get(patient=self.patient, kind=LANDMARKS_KIND)
        self.assertFalse(annotation_set.ever_annotated)
        self._save(expected_revision=self._state()["revision"])
        annotation_set.refresh_from_db()
        self.assertTrue(annotation_set.ever_annotated)

    def test_no_scratch_payload_is_stored(self):
        # A landmark *is* a point; a second copy allowed to go stale would only ever
        # disagree with the items.
        revision = self._save()
        self.assertEqual(revision.payloads.count(), 0)

    def test_the_state_of_a_patient_with_no_landmarks_is_empty_not_an_error(self):
        state = self._state()
        self.assertEqual(state["revision"], 0)
        self.assertIsNone(state["setId"])
        self.assertEqual(state["jaws"], {"upper": {}, "lower": {}})


class LandmarkInverseTests(SimpleTestCase):
    """`landmarks_from_items` on hand-built rows, with no database."""

    class _Item:
        def __init__(self, points, attributes, code, order=0):
            self.points = points
            self.attributes = attributes
            self.order = order
            self.label = type("L", (), {"code": code})()

    def test_multi_point_order_comes_from_the_stored_index_not_the_row_order(self):
        # Items carry forward as fresh rows on every revision, so their ids say when the
        # last save happened -- not which cusp came first.
        rows = [
            self._Item([[3, 3, 3]], {"landmark": "cusps", "index": 2, "fdi": "11"}, "11"),
            self._Item([[1, 1, 1]], {"landmark": "cusps", "index": 0, "fdi": "11"}, "11"),
            self._Item([[2, 2, 2]], {"landmark": "cusps", "index": 1, "fdi": "11"}, "11"),
        ]
        document = landmarks_from_items(rows, patient_id=7)
        self.assertEqual(
            document["7_upper_FDI_11"]["cusps"], [[1, 1, 1], [2, 2, 2], [3, 3, 3]]
        )

    def test_a_row_with_no_landmark_attribute_is_dropped_rather_than_guessed_at(self):
        self.assertEqual(landmarks_from_items([self._Item([[0, 0, 0]], {}, "11")], patient_id=7), {})

    def test_keys_come_back_sorted(self):
        rows = [
            self._Item([[0, 0, 0]], {"landmark": "incisal", "fdi": "31"}, "31"),
            self._Item([[0, 0, 0]], {"landmark": "incisal", "fdi": "11"}, "11"),
        ]
        self.assertEqual(
            list(landmarks_from_items(rows, patient_id=7)),
            ["7_lower_FDI_31", "7_upper_FDI_11"],
        )


class LandmarkApiTests(TestCase):
    """The two endpoints: what they write, what they refuse, and who may call them."""

    @classmethod
    def setUpTestData(cls):
        suffix = uuid.uuid4().hex[:8]
        cls.project = Project.objects.create(
            name=f"api-{suffix}", slug=f"api-{suffix}", domain="maxillo"
        )
        cls.folder = Folder.objects.create(name="mx", project=cls.project)
        cls.patient = Patient.objects.create(
            patient_id=9805, folder=cls.folder, project=cls.project
        )
        cls.upper = _mesh(cls.patient, "ios_raw_upper", "u.stl", "a")
        cls.lower = _mesh(cls.patient, "ios_raw_lower", "l.stl", "b")
        cls.project.annotation_methods.set(
            AnnotationMethod.objects.filter(slug="ios_landmarks")
        )

    def setUp(self):
        self.user = User.objects.create_user(
            username=f"u{uuid.uuid4().hex[:8]}", password="pw"  # noqa: S106
        )
        ProjectAccess.objects.create(user=self.user, project=self.project, role="annotator")
        self.client.force_login(self.user)
        self.url = reverse(
            "maxillo:api_save_ios_landmarks", kwargs={"patient_id": self.patient.patient_id}
        )
        self.state_url = reverse(
            "maxillo:api_ios_landmarks_state",
            kwargs={"patient_id": self.patient.patient_id},
        )

    def _post(self, body):
        return self.client.post(
            self.url, data=json.dumps(body), content_type="application/json"
        )

    def test_a_save_writes_the_landmarks_and_reports_the_meshes_it_anchored_to(self):
        response = self._post(
            {"meshes": [{"jaw": "upper", "landmarks": {"11": {"incisal": [1, 2, 3]}}}]}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["revision"], 1)
        self.assertEqual(body["teeth"], 1)
        # Echoed so the client can tell the mesh it drew on is the mesh the server filed
        # against -- the pair can change between the viewer loading and the save landing.
        self.assertEqual(body["meshes"], {"upper": self.upper.pk})

    def test_the_client_cannot_choose_the_mesh(self):
        # There is no fileId in this body by design: an anchor a client could pick is an
        # anchor a stale client could get wrong, and the points would then be coordinates
        # against geometry nobody was looking at.
        self._post(
            {
                "meshes": [
                    {
                        "jaw": "upper",
                        "fileId": self.lower.pk,
                        "landmarks": {"11": {"incisal": [1, 2, 3]}},
                    }
                ]
            }
        )
        state = ios_landmarks_state(self.patient, domain_field="patient")
        item = SpatialAnnotation3DItem.objects.get(
            revision__annotation_set__patient=self.patient
        )
        self.assertEqual(item.target.source_resource.file_id, self.upper.pk)
        self.assertEqual(state["jaws"]["upper"]["11"]["incisal"], [1.0, 2.0, 3.0])

    def test_the_state_endpoint_reports_the_pair_the_points_belong_to(self):
        self._post({"meshes": [{"jaw": "upper", "landmarks": {"11": {"incisal": [1, 2, 3]}}}]})
        body = self.client.get(self.state_url).json()
        self.assertEqual(body["revision"], 1)
        self.assertEqual(body["jaws"]["upper"]["11"]["incisal"], [1.0, 2.0, 3.0])
        self.assertEqual(body["meshes"], {"upper": self.upper.pk, "lower": self.lower.pk})

    def test_a_stale_revision_is_a_409_with_a_conflict_flag(self):
        self._post({"meshes": [{"jaw": "upper", "landmarks": {}}]})
        self._post({"meshes": [{"jaw": "upper", "landmarks": {}}], "expectedRevision": 1})
        response = self._post(
            {"meshes": [{"jaw": "upper", "landmarks": {}}], "expectedRevision": 1}
        )
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()["conflict"])

    def test_a_tooth_in_the_wrong_jaw_is_a_400(self):
        response = self._post(
            {"meshes": [{"jaw": "upper", "landmarks": {"31": {"incisal": [0, 0, 0]}}}]}
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(SpatialAnnotation3DItem.objects.exists())

    def test_an_unknown_jaw_is_a_400(self):
        self.assertEqual(
            self._post({"meshes": [{"jaw": "middle", "landmarks": {}}]}).status_code, 400
        )

    def test_an_empty_mesh_list_is_a_400(self):
        self.assertEqual(self._post({"meshes": []}).status_code, 400)

    def test_malformed_json_is_a_400(self):
        response = self.client.post(
            self.url, data="{not json", content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_a_reader_may_read_but_not_write(self):
        reader = User.objects.create_user(
            username=f"r{uuid.uuid4().hex[:8]}", password="pw"  # noqa: S106
        )
        ProjectAccess.objects.create(user=reader, project=self.project, role="viewer")
        self.client.force_login(reader)
        self.assertEqual(self.client.get(self.state_url).status_code, 200)
        self.assertEqual(
            self._post({"meshes": [{"jaw": "upper", "landmarks": {}}]}).status_code, 403
        )

    def test_a_project_with_the_method_switched_off_is_refused(self):
        self.project.annotation_methods.clear()
        response = self._post(
            {"meshes": [{"jaw": "upper", "landmarks": {"11": {"incisal": [1, 2, 3]}}}]}
        )
        self.assertEqual(response.status_code, 403)

    def test_a_patient_with_no_scan_pair_cannot_anchor_landmarks(self):
        lonely = Patient.objects.create(
            patient_id=9806, folder=self.folder, project=self.project
        )
        response = self.client.post(
            reverse("maxillo:api_save_ios_landmarks", kwargs={"patient_id": 9806}),
            data=json.dumps({"meshes": [{"jaw": "upper", "landmarks": {}}]}),
            content_type="application/json",
        )
        # 409 rather than 400: the request is well formed, the patient is not ready for it.
        self.assertEqual(response.status_code, 409)
        self.assertFalse(
            AnnotationSet.objects.filter(patient=lonely, kind=LANDMARKS_KIND).exists()
        )


class WorkerDocumentTests(SimpleTestCase):
    """The landmark job's output, on its way into the record."""

    def test_worker_keys_are_rewritten_to_canonical_ones(self):
        # The worker's input is one scan pair, so it has no patient id to put in the key.
        document = normalize_worker_document(
            {"in_upper_FDI_11": {"incisal": [1, 2, 3]}}, patient_id=7
        )
        self.assertEqual(document, {"7_upper_FDI_11": {"incisal": [1, 2, 3]}})

    def test_a_landmarks_envelope_is_unwrapped(self):
        document = normalize_worker_document(
            {"landmarks": {"in_lower_FDI_31": {"gingival": [0, 0, 0]}}}, patient_id=7
        )
        self.assertEqual(list(document), ["7_lower_FDI_31"])

    def test_another_patients_key_is_dropped_rather_than_refused(self):
        """The opposite of `ios_landmarks`, and deliberately so.

        A *human* save carrying another patient's key is a client bug worth refusing. A
        worker aggregate legitimately covers several scans, and the caller wants this
        patient's share of it.
        """
        document = normalize_worker_document(
            {"8_upper_FDI_11": {"incisal": [0, 0, 0]}, "in_upper_FDI_21": {"outer": [1, 1, 1]}},
            patient_id=7,
        )
        self.assertEqual(list(document), ["7_upper_FDI_21"])

    def test_a_zero_padded_patient_id_still_matches(self):
        """The legacy read path compared numerically, and a worker may zero-pad.

        Dropping a whole arch of predictions over a leading zero would be a silent
        regression -- the strict converter is right to compare as text, but this path
        reads machine output and the legacy one was lenient here.
        """
        document = normalize_worker_document(
            {"012_upper_FDI_11": {"incisal": [1, 2, 3]}}, patient_id=12
        )
        self.assertEqual(list(document), ["12_upper_FDI_11"])

    def test_a_non_object_payload_is_refused(self):
        with self.assertRaises(ValidationError):
            normalize_worker_document([1, 2, 3], patient_id=7)

    def test_by_jaw_splits_a_document_into_the_wire_format(self):
        jaws = by_jaw(
            {
                "7_upper_FDI_11": {"incisal": [1, 2, 3]},
                "7_lower_FDI_31": {"gingival": [4, 5, 6]},
            }
        )
        self.assertEqual(jaws["upper"], {"11": {"incisal": [1, 2, 3]}})
        self.assertEqual(jaws["lower"], {"31": {"gingival": [4, 5, 6]}})


class PredictedLandmarkTests(TestCase):
    """The prediction writer's gate: model output must never overwrite hand-placed work."""

    @classmethod
    def setUpTestData(cls):
        suffix = uuid.uuid4().hex[:8]
        cls.project = Project.objects.create(
            name=f"pred-{suffix}", slug=f"pred-{suffix}", domain="maxillo"
        )
        cls.folder = Folder.objects.create(name="mx", project=cls.project)
        cls.patient = Patient.objects.create(
            patient_id=9810, folder=cls.folder, project=cls.project
        )
        cls.upper = _mesh(cls.patient, "ios_raw_upper", "u.stl", "a")
        cls.lower = _mesh(cls.patient, "ios_raw_lower", "l.stl", "b")
        cls.project.annotation_methods.set(
            AnnotationMethod.objects.filter(slug="ios_landmarks")
        )

    def test_a_patient_with_hand_placed_landmarks_is_left_alone(self):
        from maxillo.file_utils import _record_predicted_landmarks

        save_ios_landmarks(
            self.patient,
            meshes=[
                {
                    "file_obj": self.upper,
                    "jaw": "upper",
                    "landmarks": {"11": {"incisal": [1, 2, 3]}},
                }
            ],
        )
        before = ios_landmarks_state(self.patient, domain_field="patient")

        # A path that does not exist: the gate must return before it is ever read, so this
        # standing in for object storage is the assertion.
        _record_predicted_landmarks(self.patient, "does/not/exist.json", job=None)

        after = ios_landmarks_state(self.patient, domain_field="patient")
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["document"], before["document"])

    def test_a_prediction_never_fails_the_job(self):
        # A malformed document, an unreachable bucket or a missing scan pair are all
        # reasons not to write landmarks, and none is a reason to fail a completion the
        # runner would retry forever against a gate that will not move.
        from maxillo.file_utils import _record_predicted_landmarks

        _record_predicted_landmarks(self.patient, "does/not/exist.json", job=None)


class ConvertedStudyReanchorTests(TestCase):
    """A study converted from the legacy document, then edited live.

    `annotations_materialize_landmarks` anchors a converted document to the JSON *file*,
    because the legacy artifact named the patient and never the mesh. Phase 6 writes mesh
    targets. So every converted study crosses that boundary the first time somebody edits
    it, and this is what happens when it does.

    The failure this guards against is not hypothetical: `save_measurement_groups` carries
    forward every target a save did not name, so an untouched legacy anchor would have its
    items copied alongside the freshly written mesh ones. Single-point types survive that
    (the same value written twice); `cusps` and `planar` are lists the reader appends to,
    and would have doubled on the first edit of every converted study.
    """

    ENTRY = {
        "incisal": [1.0, 2.0, 3.0],
        "cusps": [[1.0, 1.0, 1.0], [2.0, 2.0, 2.0]],
    }

    @classmethod
    def setUpTestData(cls):
        suffix = uuid.uuid4().hex[:8]
        cls.project = Project.objects.create(
            name=f"conv-{suffix}", slug=f"conv-{suffix}", domain="maxillo"
        )
        cls.folder = Folder.objects.create(name="mx", project=cls.project)
        cls.patient = Patient.objects.create(
            patient_id=9820, folder=cls.folder, project=cls.project
        )
        cls.upper = _mesh(cls.patient, "ios_raw_upper", "u.stl", "a")
        cls.lower = _mesh(cls.patient, "ios_raw_lower", "l.stl", "b")
        cls.document_file = _mesh(cls.patient, "ios_landmarks", "landmarks.json", "c")
        cls.project.annotation_methods.set(
            AnnotationMethod.objects.filter(slug="ios_landmarks")
        )

    def setUp(self):
        """Reproduce what `annotations_materialize_landmarks` leaves behind."""
        from annotations.adapters.ios_landmarks import ios_landmarks as convert
        from annotations.services.apply import apply_descriptors
        from annotations.services.ios_landmarks import LANDMARK_DOCUMENT_ROLE
        from annotations.services.labels import fdi_schema
        from annotations.services.resources import register_file
        from annotations.services.sets import attach_target, get_or_create_set, record_revision

        annotation_set = get_or_create_set(
            self.patient, LANDMARKS_KIND, label_schema=fdi_schema(), check_project=False
        )
        resource = register_file(self.document_file, content_hash=self.document_file.file_hash)
        target = attach_target(
            annotation_set, resource, role=LANDMARK_DOCUMENT_ROLE, primary=True
        )
        revision = record_revision(
            annotation_set,
            author=None,
            origin=AnnotationOrigin.MIGRATION,
            note="legacy:maxillo.ios_landmarks:1",
        )
        document = {
            "9820_upper_FDI_11": dict(self.ENTRY),
            "9820_lower_FDI_31": {"gingival": [9.0, 9.0, 9.0]},
        }
        apply_descriptors(
            revision, target, convert(document, patient_id=9820), require_labels=True
        )
        self.converted = document

    def _state(self):
        return ios_landmarks_state(self.patient, domain_field="patient")

    def test_a_converted_study_reads_back_without_being_re_anchored_first(self):
        # The reader groups by the arch the FDI code implies, not by the target's role,
        # which is what lets one read path serve both anchors.
        state = self._state()
        self.assertEqual(state["document"], self.converted)
        self.assertEqual(state["jaws"]["upper"]["11"]["cusps"], self.ENTRY["cusps"])

    def test_editing_both_jaws_re_anchors_the_study_without_doubling_anything(self):
        before = self._state()
        save_ios_landmarks(
            self.patient,
            meshes=[
                {"file_obj": self.upper, "jaw": "upper", "landmarks": before["jaws"]["upper"]},
                {"file_obj": self.lower, "jaw": "lower", "landmarks": before["jaws"]["lower"]},
            ],
            expected_revision=before["revision"],
        )
        after = self._state()
        self.assertEqual(
            after["document"], self.converted, "the document survived the re-anchor"
        )
        self.assertEqual(
            after["jaws"]["upper"]["11"]["cusps"],
            self.ENTRY["cusps"],
            "cusps doubled: the legacy anchor was carried forward alongside the mesh one",
        )

    def test_the_re_anchored_items_hang_off_the_mesh_targets(self):
        before = self._state()
        revision = save_ios_landmarks(
            self.patient,
            meshes=[
                {"file_obj": self.upper, "jaw": "upper", "landmarks": before["jaws"]["upper"]},
                {"file_obj": self.lower, "jaw": "lower", "landmarks": before["jaws"]["lower"]},
            ],
            expected_revision=before["revision"],
        )
        roles = {
            item.target.role
            for item in SpatialAnnotation3DItem.objects.filter(
                revision=revision
            ).select_related("target")
        }
        self.assertEqual(roles, {MESH_ROLES["upper"], MESH_ROLES["lower"]})

    def test_editing_one_jaw_leaves_the_other_on_the_old_anchor_rather_than_deleting_it(self):
        # A partial save must not delete landmarks the client never sent and could not
        # have known to send.
        before = self._state()
        save_ios_landmarks(
            self.patient,
            meshes=[
                {"file_obj": self.upper, "jaw": "upper", "landmarks": before["jaws"]["upper"]}
            ],
            expected_revision=before["revision"],
        )
        after = self._state()
        self.assertEqual(after["document"], self.converted)
        self.assertEqual(after["jaws"]["lower"], before["jaws"]["lower"])
