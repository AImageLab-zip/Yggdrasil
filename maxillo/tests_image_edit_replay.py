"""The server half of the image-edit replay, against the shared fixture.

The implementation moved to ``annotations.adapters.image_edit_replay`` in Phase 5 -- the
module that used to hold it was a views module whose endpoints the Cornerstone editor
replaced, and both the annotation read path and the export need it now. The cases are
unchanged; only the import moved.

`rgb_editor.js` can crop, mirror and rotate an intraoral photograph, and every tooth
polygon already drawn on it is expressed in the *old* pixel frame. Two implementations
re-project them -- this one on the read path, and
`frontend/imaging/photos/editReplay.js` for the live preview -- and they had drifted:
this one implemented `flip-h`, `flip-v` and `crop` and neither rotate case, so a rotated
photograph read back untransformed polygons and the segmentation silently detached from
the anatomy.

`common/fixtures/image_edit_replay.json` is now the contract, read by this module and by
`frontend/tests/editReplay.test.js`. A change to either implementation that is not
matched in the other fails on both sides.
"""

import json
from pathlib import Path

from django.test import SimpleTestCase

from annotations.adapters.image_edit_replay import transform_polygon, transform_teeth

FIXTURE = Path(__file__).resolve().parent.parent / "common" / "fixtures" / "image_edit_replay.json"


def load_cases():
    return json.loads(FIXTURE.read_text())["cases"]


class SharedEditReplayFixtureTests(SimpleTestCase):
    def test_the_fixture_is_present_and_has_cases(self):
        """A vanished fixture must fail loudly rather than pass zero cases silently."""
        cases = load_cases()
        self.assertGreaterEqual(len(cases), 10)
        names = [case["name"] for case in cases]
        self.assertEqual(len(names), len(set(names)), "case names must be unique")

    def test_every_case_matches(self):
        for case in load_cases():
            with self.subTest(case=case["name"]):
                self.assertEqual(
                    transform_polygon(case["polygon"], case["operations"]),
                    [list(point) for point in case["expected"]],
                )

    def test_the_rotate_cases_are_actually_exercised(self):
        """The two operations this implementation was missing.

        Asserted explicitly rather than left to the loop above, because the failure being
        prevented is not a wrong number -- it is the identity transform quietly standing
        in for a rotation, which every non-rotate case would still pass.
        """
        covered = {
            operation["type"]
            for case in load_cases()
            for operation in case["operations"]
        }
        self.assertIn("rotate-cw", covered)
        self.assertIn("rotate-arbitrary", covered)

    def test_a_rotation_actually_moves_the_polygon(self):
        rotated = transform_polygon(
            [[10, 20], [30, 20], [30, 40]],
            [{"type": "rotate-cw", "input_width": 100, "input_height": 80}],
        )
        self.assertNotEqual(
            rotated, [[10, 20], [30, 20], [30, 40]],
            "before the fix this returned its input, which is the whole bug",
        )


class TransformTeethTests(SimpleTestCase):
    def test_a_tooth_whose_polygons_all_vanish_is_dropped(self):
        teeth = {"11": [[[10, 10], [20, 10], [20, 20], [10, 20]]]}
        out = transform_teeth(
            teeth, {"operations": [{"type": "crop", "x": 50, "y": 50, "width": 20, "height": 20}]}
        )
        self.assertEqual(out, {}, "a crop can remove a tooth from the picture entirely")

    def test_no_operations_returns_the_geometry_unchanged(self):
        teeth = {"11": [[[10, 10], [20, 10], [20, 20]]]}
        self.assertEqual(transform_teeth(teeth, {"operations": []}), teeth)
        self.assertEqual(transform_teeth(teeth, None), teeth)

    def test_replay_is_idempotent_from_the_pristine_geometry(self):
        """The property the whole preview path depends on.

        The editor replays from `baseTeeth` on every keystroke rather than composing onto
        its own output; if that were not idempotent the polygons would drift a little
        further with every preview.
        """
        teeth = {"11": [[[10, 20], [30, 20], [30, 40]]]}
        edit = {"operations": [{"type": "flip-h", "input_width": 100, "input_height": 80}]}
        once = transform_teeth(teeth, edit)
        self.assertEqual(transform_teeth(teeth, edit), once)
