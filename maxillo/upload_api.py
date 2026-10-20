"""Maxillo's part of the upload API.

Field names are the web form's (maxillo/forms.py): ``cbct``, ``ios_upper`` +
``ios_lower`` (stored together as one ``ios`` modality), ``intraoral-photos``
(several files of the ``intraoral-photo`` modality), and the single-file
``teleradiography`` / ``panoramic`` / ``rawzip``. The first API used other names
for IOS and photos; they are still accepted as aliases.
"""

from django.core.exceptions import ValidationError

from common.domain_uploads import UploadField, UploadHandler, UploadRejected

MAX_INTRAORAL_PHOTOS = 10
GENERIC_SLUGS = ("teleradiography", "panoramic", "rawzip")


class MaxilloUploadHandler(UploadHandler):
    upload_fields = (
        UploadField("cbct", "cbct"),
        UploadField("ios_upper", "ios", together_with=("ios_lower",)),
        UploadField("ios_lower", "ios", together_with=("ios_upper",)),
        UploadField("intraoral-photos", "intraoral-photo", multiple=True),
        *(UploadField(slug, slug) for slug in GENERIC_SLUGS),
    )
    aliases = {
        "upper_scan_raw": "ios_upper",
        "lower_scan_raw": "ios_lower",
        "intraoral_photos": "intraoral-photos",
    }

    def validate(self, files, accepted):
        super().validate(files, accepted)
        if len(files.get("intraoral-photos", ())) > MAX_INTRAORAL_PHOTOS:
            raise UploadRejected(f"Send no more than {MAX_INTRAORAL_PHOTOS} intraoral photographs")

    def save(self, patient, files, accepted, result):
        from . import file_utils

        modalities = {spec.name: modality for spec, modality in accepted}

        def sent(name):
            return name in files and name in modalities

        if sent("cbct"):
            patient.modalities.add(modalities["cbct"])
            try:
                _key, job = file_utils.save_cbct_to_dataset(patient, files["cbct"][0])
            except ValidationError as exc:
                raise UploadRejected("; ".join(exc.messages)) from exc
            result.messages.append("CBCT scan queued for processing")
            result.add_job(job, "cbct")

        if sent("ios_upper") and sent("ios_lower"):
            patient.modalities.add(modalities["ios_upper"])
            ios = file_utils.save_ios_to_dataset(
                patient, files["ios_upper"][0], files["ios_lower"][0]
            )
            result.messages.append("IOS scans queued for processing")
            result.add_job(ios.get("processing_job"), "ios")
            result.add_job(ios.get("bite_classification_job"), "bite_classification")

        if sent("intraoral-photos"):
            patient.modalities.add(modalities["intraoral-photos"])
            saved, errors, job = file_utils.save_intraoral_photos_to_dataset(
                patient, files["intraoral-photos"]
            )
            result.messages.append(f"Uploaded {len(saved)} intraoral image(s)")
            result.messages.extend(errors)
            result.add_job(job, "intraoral-photo")

        for slug in GENERIC_SLUGS:
            if sent(slug):
                patient.modalities.add(modalities[slug])
                _registry, job = file_utils.save_generic_modality_file(patient, slug, files[slug][0])
                result.messages.append(f"{modalities[slug].name} uploaded")
                result.add_job(job, slug)
