"""What each domain accepts on an upload, and how it stores it.

The upload API (``common/api/project_data.py``) is the same for every domain up
to the moment files reach storage, and that moment is each domain's own: brain
writes NIfTI volumes, cardiology validates an ECG document, maxillo pairs two IOS
arches. A domain app describes its part with an :class:`UploadHandler` and
registers it from its ``AppConfig.ready()``; ``common`` never imports a domain
app (import-linter), so this registry is how the API reaches them.

A domain with no handler registered cannot be uploaded to over the API. It is
refused (:func:`get_handler` returns ``None``) rather than served by another
domain's code -- which is how the first version of the API wrote brain uploads
into maxillo's tables.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class UploadField:
    """One multipart field of an upload.

    ``name`` is the form field the client sends; ``modality`` the slug of the
    ``Modality`` it stores, which the project must enable. They are equal for
    almost every modality -- the exceptions are files that only make sense
    together (maxillo's two IOS arches) or several files of one modality.
    """

    name: str
    modality: str
    multiple: bool = False
    #: Other field names that must be sent together with this one.
    together_with: tuple = ()
    #: Extra fields that do not name a modality of their own and ride along with
    #: this one (urology's optional segmentation overlay).
    companions: tuple = ()

    def describe(self, modality=None):
        return {
            "name": self.name,
            "modality": self.modality,
            "multiple": self.multiple,
            "together_with": list(self.together_with),
            "companions": list(self.companions),
            "supported_extensions": list(getattr(modality, "supported_extensions", None) or []),
        }


class UploadRejected(Exception):
    """A request the handler refuses before anything is stored. Becomes a 400."""

    def __init__(self, message, **extra):
        super().__init__(message)
        self.extra = extra


@dataclass
class UploadResult:
    messages: list = field(default_factory=list)
    jobs: list = field(default_factory=list)

    def add_job(self, job, kind):
        if job is not None:
            self.jobs.append({"id": job.id, "type": kind, "status": getattr(job, "status", None)})

    def as_dict(self):
        return {"messages": self.messages, "jobs": self.jobs}


class UploadHandler:
    """Base class for a domain's upload handler.

    Subclasses list their :attr:`upload_fields` and implement :meth:`save`.
    """

    upload_fields: tuple = ()
    #: Old field names still accepted: ``{old_name: field_name}``.
    aliases: dict = {}

    def fields_for(self, project):
        """The fields this project accepts: those whose modality it enables.

        A project that enables no modality accepts nothing. The web form has a
        permissive fallback for that case; the API does not, because a script
        cannot see which of its files were dropped.
        """
        enabled = {
            m.slug: m for m in project.modalities.filter(is_active=True)
        }
        return [(f, enabled[f.modality]) for f in self.upload_fields if f.modality in enabled]

    def validate(self, files, accepted):
        """Refuse a request before the patient row exists.

        ``files`` maps field name -> list of uploaded files, already limited to
        the accepted fields; ``accepted`` is :meth:`fields_for`'s
        ``[(UploadField, Modality)]``.
        """
        for spec, _modality in accepted:
            if spec.name in files and not spec.multiple and len(files[spec.name]) > 1:
                raise UploadRejected(f"'{spec.name}' takes a single file")
            missing = [
                other for other in spec.together_with
                if spec.name in files and other not in files
            ]
            if missing:
                raise UploadRejected(
                    f"'{spec.name}' must be sent together with: {', '.join(missing)}"
                )

    def save(self, patient, files, accepted, result):
        """Store ``files`` for ``patient`` and add the modalities they belong to.

        Runs inside the API's transaction: raising rolls back the patient row.
        ``result`` is an :class:`UploadResult` to report jobs and messages into.
        """
        raise NotImplementedError


class PerModalityHandler(UploadHandler):
    """A domain whose every field is one file of one modality, stored on its own.

    Covers brain, urology, laparoscopy and cardiology: subclasses implement
    :meth:`save_one` and return ``(file_registry, job)``.
    """

    def save(self, patient, files, accepted, result):
        for spec, modality in accepted:
            uploaded = files.get(spec.name)
            if not uploaded:
                continue
            patient.modalities.add(modality)
            _registry, job = self.save_one(patient, spec, uploaded[0], files)
            result.messages.append(f"{modality.name} uploaded")
            result.add_job(job, spec.modality)

    def save_one(self, patient, spec, uploaded_file, files):
        raise NotImplementedError


_HANDLERS = {}


def register(domain, handler):
    from common.domains import DOMAINS

    if domain not in DOMAINS:
        raise ValueError(f"Unknown domain {domain!r}")
    _HANDLERS[domain] = handler


def get_handler(domain):
    return _HANDLERS.get(domain)


def registered_domains():
    return frozenset(_HANDLERS)
