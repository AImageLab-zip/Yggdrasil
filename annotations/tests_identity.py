"""``identity_key`` construction -- pure, so tested without touching the database.

The column these strings go into is unique and unconditional, which makes it the
only thing standing between "one resource per set of bytes" and two resources
splitting one volume's annotations between them. Everything worth asserting here
is about *collisions* and *stability*: two spellings of the same thing must
produce one key, two different things must never produce the same one, and a
value that would be silently truncated by the column must be an error instead.
"""

from django.test import SimpleTestCase

from annotations import identity
from annotations.identity import IdentityError


class FileIdentityTests(SimpleTestCase):
    def test_a_missing_key_and_an_explicit_primary_are_the_same_resource(self):
        """``maxillo.api_views.files`` already treats an absent key as primary."""
        self.assertEqual(identity.for_file(412), identity.for_file(412, "primary"))
        self.assertEqual(identity.for_file(412), identity.for_file(412, ""))
        self.assertEqual(identity.for_file(412), identity.for_file(412, None))

    def test_a_bundle_member_is_a_different_resource_from_its_bundle(self):
        bundle = identity.for_file(412)
        member = identity.for_file(412, "segmentation_nifti")

        self.assertNotEqual(bundle, member)

    def test_two_members_of_one_bundle_do_not_collide(self):
        self.assertNotEqual(
            identity.for_file(412, "volume_nifti"),
            identity.for_file(412, "segmentation_nifti"),
        )

    def test_the_id_is_normalized_so_an_int_and_a_string_agree(self):
        self.assertEqual(identity.for_file(412), identity.for_file("412"))

    def test_a_missing_id_is_an_error_rather_than_a_key_ending_in_none(self):
        for bad in (None, "", "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(IdentityError):
                    identity.for_file(bad)


class SeparatorTests(SimpleTestCase):
    """A part containing a separator would make the key ambiguous."""

    def test_a_file_key_containing_a_separator_is_refused(self):
        for bad in ("volume/nifti", "volume:nifti"):
            with self.subTest(bad=bad):
                with self.assertRaises(IdentityError):
                    identity.for_file(412, bad)

    def test_a_file_key_containing_a_separator_is_refused_for_a_volume_too(self):
        with self.assertRaises(IdentityError):
            identity.for_logical_volume(412, "volume:nifti")


class LogicalVolumeTests(SimpleTestCase):
    def test_the_volume_and_the_file_are_never_the_same_key(self):
        """Same bytes, two roles: only the volume promises a stable voxel grid."""
        self.assertNotEqual(identity.for_file(412), identity.for_logical_volume(412))
        self.assertNotEqual(
            identity.for_file(412, "volume_nifti"),
            identity.for_logical_volume(412, "volume_nifti"),
        )

    def test_the_primary_normalization_matches_the_file_case(self):
        self.assertEqual(
            identity.for_logical_volume(412), identity.for_logical_volume(412, "primary")
        )


class DerivedResourceTests(SimpleTestCase):
    def test_a_derived_resource_names_what_it_came_from(self):
        source = identity.for_logical_volume(412, "volume_nifti")

        key = identity.for_derived_resource("panorex-js-v2", source, "mip")

        self.assertIn(source, key)
        self.assertIn("panorex-js-v2", key)

    def test_two_discriminators_of_one_source_do_not_collide(self):
        source = identity.for_logical_volume(412)

        self.assertNotEqual(
            identity.for_derived_resource("panorex-js-v2", source, "mip"),
            identity.for_derived_resource("panorex-js-v2", source, "raysum"),
        )

    def test_the_same_discriminator_from_two_sources_does_not_collide(self):
        self.assertNotEqual(
            identity.for_derived_resource(
                "panorex-js-v2", identity.for_logical_volume(1), "mip"
            ),
            identity.for_derived_resource(
                "panorex-js-v2", identity.for_logical_volume(2), "mip"
            ),
        )

    def test_a_source_key_is_required(self):
        with self.assertRaises(IdentityError):
            identity.for_derived_resource("panorex-js-v2", "")


class LengthTests(SimpleTestCase):
    """The column is ``varchar(255)``; a longer key must not reach it.

    MySQL in non-strict mode truncates silently, and a truncated key can collide
    with another truncated key -- two unrelated resources merged into one, which
    is precisely the failure the unique column exists to prevent.
    """

    def test_an_over_long_key_is_refused_rather_than_truncated(self):
        source = identity.for_logical_volume(412)

        with self.assertRaises(IdentityError) as caught:
            identity.for_derived_resource("x" * 300, source)

        self.assertIn("255", str(caught.exception))

    def test_a_key_at_the_limit_is_accepted(self):
        # 'derived_resource:' is 17 characters, then the producer, a '/', and the
        # source key -- padded here to land exactly on the limit.
        source = identity.for_logical_volume(412)
        padding = identity.MAX_IDENTITY_KEY_LENGTH - len("derived_resource:") - 1 - len(source)

        key = identity.for_derived_resource("p" * padding, source)

        self.assertEqual(len(key), identity.MAX_IDENTITY_KEY_LENGTH)
