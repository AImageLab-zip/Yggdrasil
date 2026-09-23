"""Slide label and macro images never reach the viewer.

Scanner files store a photo of the slide label (and of the whole glass) next to the
tissue pyramid, and those routinely show the patient's name or barcode. Every IFD
used to be offered as a pyramid level, and the minimap thumbnail was taken from the
last IFD -- which in an Aperio SVS is exactly the macro or label image.
"""

import io
import tempfile

from django.test import SimpleTestCase
from PIL import Image, ImageDraw
from PIL.TiffImagePlugin import ImageFileDirectory_v2

from urology import wsi_reader

LABEL_COLOUR = (255, 0, 0)
TISSUE_COLOUR = (230, 200, 220)


def _slide(label_size=(256, 128), label_description=None):
    pages = [Image.new("RGB", (s, s), TISSUE_COLOUR) for s in (1024, 512, 256)]
    label = Image.new("RGB", label_size, LABEL_COLOUR)
    ImageDraw.Draw(label).text((5, 5), "PATIENT NAME", fill=(0, 0, 0))
    path = tempfile.NamedTemporaryFile(suffix=".tif", delete=False).name
    pages[0].save(
        path, format="TIFF", save_all=True, append_images=pages[1:] + [label], tiled=True,
    )
    if label_description:
        # Pillow writes one description for all pages; rewrite the label IFD's own.
        with Image.open(path) as img:
            frames = []
            for i in range(img.n_frames):
                img.seek(i)
                frames.append(img.copy())
        descriptions = ["Aperio Image Library |", "", "", label_description]
        _save_with_descriptions(path, frames, descriptions)
    return path


def _save_with_descriptions(path, frames, descriptions):
    from PIL import TiffImagePlugin

    with TiffImagePlugin.AppendingTiffWriter(path, new=True) as tf:
        for frame, description in zip(frames, descriptions):
            ifd = ImageFileDirectory_v2()
            if description:
                ifd[270] = description
            frame.save(tf, format="TIFF", tiffinfo=ifd)
            tf.newFrame()


class SlideLabelTests(SimpleTestCase):
    def test_a_differently_shaped_label_is_not_a_level(self):
        path = _slide()
        frames, n_frames = wsi_reader.pyramid_frames(path)
        self.assertEqual(n_frames, 4)
        self.assertEqual(frames, (0, 1, 2))
        levels = [lvl["level"] for lvl in wsi_reader.get_wsi_metadata(path)["levels"]]
        self.assertEqual(levels, [0, 1, 2])

    def test_a_label_ifd_described_as_such_is_refused_even_if_square(self):
        path = _slide(label_size=(128, 128), label_description="label 128x128")
        frames, _ = wsi_reader.pyramid_frames(path)
        self.assertNotIn(3, frames)

    def test_requesting_the_label_ifd_directly_returns_a_blank_tile(self):
        path = _slide()
        tile = Image.open(io.BytesIO(wsi_reader.get_wsi_tile(path, level=3, col=0, row=0, image_format="PNG")))
        self.assertNotIn(LABEL_COLOUR, [c for _, c in tile.convert("RGB").getcolors(1 << 16)])

    def test_the_thumbnail_is_the_tissue_not_the_label(self):
        path = _slide()
        thumb = Image.open(io.BytesIO(wsi_reader.get_wsi_thumbnail(path, image_format="PNG"))).convert("RGB")
        self.assertEqual(thumb.size, (256, 256))
        self.assertEqual(thumb.getpixel((128, 128)), TISSUE_COLOUR)
