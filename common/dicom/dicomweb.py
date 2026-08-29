"""Turn a stored instance into what Cornerstone's ``wadors`` loader asks for.

Two answers, and the shapes are dictated by the shipped
``@cornerstonejs/dicom-image-loader@5.8.2`` rather than chosen -- each one was read out
of the package, and ``frontend/tests/dicomImageIds.test.js`` pins the half that lives in
the browser.

**Metadata** is the DICOM JSON model: an object keyed by eight hex digits, each
``{"vr": ..., "Value": [...]}``. ``imageLoader/wadors/metaData/metaDataProvider.js``
reads it tag by tag (``'00280030'`` for pixel spacing, ``'00200032'`` for position, and
so on), so it is produced by ``pydicom``'s own ``to_json_dict`` rather than assembled
by hand -- one fewer place to spell a tag wrong.

**Frames** are the *pixel bytes for one frame*, not the file. Two facts from
``loadImage.js`` shape the response:

* ``getTransferSyntaxForContentType`` derives the transfer syntax **from the response
  Content-Type**, falling back to Implicit VR Little Endian. So the header carries an
  explicit ``transfer-syntax=`` parameter; without it a JPEG Lossless frame would be
  handed to the raw decoder and render as noise, with no error anywhere.
* ``extractMultipart`` takes the *whole body* as pixel data when the content type is
  not multipart. So the response is a plain octet stream rather than a
  ``multipart/related`` envelope: fewer bytes, no boundary to get wrong, and it streams.

That last point is a deliberate departure from strict WADO-RS. These endpoints exist to
feed the viewer this repository ships, not to be a DICOMweb server -- Phase 8's rule is
that DICOM works as well as NIfTI and no better.
"""

import io

from pydicom import dcmread
from pydicom.encaps import generate_frames
from pydicom.uid import UID

#: Elements never serialised into the metadata response: the pixels themselves, which
#: are fetched frame by frame, and would otherwise be base64'd into every listing.
_BULK_TAGS = ("7FE00010",)


def instance_metadata(dataset):
    """The DICOM JSON model for one instance, without its pixel data.

    :param dataset: a ``pydicom`` dataset read from storage.
    :returns: ``dict`` keyed by uppercase 8-hex-digit tags.
    """
    document = dataset.to_json_dict(suppress_invalid_tags=True)
    for tag in _BULK_TAGS:
        document.pop(tag, None)
    return document


def _native_frame(dataset, frame_number):
    """One frame out of unencapsulated pixel data, by arithmetic.

    Every value below is a required element for an image IOD, so a missing one is a
    malformed instance rather than something to default.
    """
    rows = int(dataset.Rows)
    columns = int(dataset.Columns)
    samples = int(getattr(dataset, "SamplesPerPixel", 1) or 1)
    allocated = int(dataset.BitsAllocated)
    if allocated % 8:
        # 1-bit packed images (BitsAllocated=1) do not slice on a byte boundary. Not
        # supported rather than silently mis-sliced; no CBCT or CT produces one.
        raise ValueError(
            f"BitsAllocated={allocated} is not a whole number of bytes, so frames "
            "cannot be addressed individually."
        )
    frame_size = rows * columns * samples * (allocated // 8)
    payload = dataset.PixelData
    start = (frame_number - 1) * frame_size
    end = start + frame_size
    if start < 0 or end > len(payload):
        raise IndexError(f"frame {frame_number} is outside this instance")
    return payload[start:end]


def frame_bytes(dataset, frame_number):
    """The pixel bytes of one 1-based frame, encoded as they are stored.

    Never re-encoded: a compressed frame is handed back in its own syntax and the
    browser's codec decodes it, which is both faster and the only way the stored bytes
    stay the authority on what the image is.
    """
    if frame_number < 1:
        raise IndexError("frame numbers are 1-based")

    transfer_syntax = UID(
        str(getattr(getattr(dataset, "file_meta", None), "TransferSyntaxUID", "") or "")
        or "1.2.840.10008.1.2"
    )
    if not transfer_syntax.is_encapsulated:
        return _native_frame(dataset, frame_number)

    total = int(getattr(dataset, "NumberOfFrames", 1) or 1)
    for index, frame in enumerate(
        generate_frames(dataset.PixelData, number_of_frames=total), start=1
    ):
        if index == frame_number:
            return frame
    raise IndexError(f"frame {frame_number} is outside this instance")


def read_stored_instance(payload):
    """Parse bytes fetched from object storage into a dataset.

    ``force=True`` because the ingest wrote the file and its preamble; a read that
    refuses here would mean the stored object is not what this code put there, which
    is a storage problem rather than a parsing decision.
    """
    return dcmread(io.BytesIO(payload), force=True)


def content_type_for(transfer_syntax_uid):
    """The Content-Type that tells the loader how the frame is encoded.

    ``loadImage.getTransferSyntaxForContentType`` parses this parameter; with no
    parameter it assumes Implicit VR Little Endian, which is right for an
    unencapsulated frame and silently wrong for every other kind.
    """
    syntax = str(transfer_syntax_uid or "").strip() or "1.2.840.10008.1.2"
    return f"application/octet-stream; transfer-syntax={syntax}"
