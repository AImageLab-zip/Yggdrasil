"""Deleting things in the project tree, and what each delete is allowed to take.

Two very different rules live here, and keeping them side by side is the point:

* **Deleting a project destroys everything below it** -- folders, patients,
  files, annotations. It is the top of the ownership tree.
* **Deleting a folder destroys nothing but the folder.** A folder is a
  sub-organization *inside* a project; its patients belong to the project, not
  to it, and they survive with ``folder`` set to NULL. This is the invariant
  three domains previously each had their own chance to get wrong.

A ``Project`` is the top of the ownership tree: its folders, its patients, the
files of those patients and the annotations drawn on those files all belong to
it. Django's cascade alone cannot take it down, because the annotation graph
guards the bytes it was drawn on with ``PROTECT`` --
``SourceResource.file``, ``AnnotationPayload.file`` and
``AnnotationTarget.source_resource``. ``PROTECT``, unlike ``RESTRICT``, raises
even when the protecting row is part of the very same delete, so a plain
``project.delete()`` fails with ``ProtectedError`` for any project whose
patients have ever been annotated -- which is what made "delete project"
silently do nothing in the Django admin.

The guards are deliberate (see ``annotations/models/resources.py``): destroying
annotation work has to be an explicit decision. This module *is* that decision,
made in one place, in dependency order:

Project deletion runs:

1. the annotation *items* of the project's sets, which PROTECT the targets and
   selectors they are anchored to;
2. the project's ``AnnotationSet`` rows -- cascading to revisions, targets,
   selectors and payloads, which releases the holds on the files;
3. the ``SourceResource`` rows naming the project's files, now unreferenced;
4. the project itself -- cascading to folders, patients, files, jobs, access
   rows and invitations;
5. the objects in storage, by the keys the ``FileRegistry`` rows recorded.

Storage comes last on purpose: bytes left behind by a failed sweep are
reclaimable, rows pointing at deleted bytes are not.
"""

import logging

from django.apps import apps
from django.db import transaction
from django.db.models import Q

from common.domains import fk_fields_for

logger = logging.getLogger(__name__)

#: The concrete annotation item models. They are the actual annotation work, so
#: the confirmation page names them -- and because each PROTECTs its target and
#: selector, the delete has to take them explicitly before the set.
def _item_models():
    from annotations.models import (
        EventAnnotationItem,
        Geometry2DItem,
        MeasurementItem,
        SpatialAnnotation3DItem,
        TemporalAnnotationItem,
    )

    return (
        Geometry2DItem,
        SpatialAnnotation3DItem,
        MeasurementItem,
        TemporalAnnotationItem,
        EventAnnotationItem,
    )


def patients_of(project):
    """The project's patients, from its own domain's table.

    Includes soft-deleted rows: ``Patient.objects`` hides ``deleted=True``
    behind :class:`~common.base_models.ActivePatientManager`, and a soft-deleted
    patient is still a row the database cascade will take, still holding files
    and annotations. Counting only the visible ones would under-report the
    damage on the confirmation page and leave PROTECT holds in place.
    """
    Patient = apps.get_model(project.domain, "Patient")
    manager = getattr(Patient, "all_objects", None) or Patient._base_manager
    return manager.filter(project_id=project.id)


def folders_of(project):
    Folder = apps.get_model(project.domain, "Folder")
    return Folder.objects.filter(project_id=project.id)


def files_of(project):
    """Every ``FileRegistry`` row the project's cascade will delete.

    A file hangs off a patient directly, off one of that patient's voice
    captions, or off a job run for the patient -- three CASCADE paths, all of
    which the project delete walks, so all three have to be released.
    """
    from common.models import FileRegistry

    patient_fk, voice_fk = fk_fields_for(project.domain)
    return FileRegistry.objects.filter(
        Q(**{f"{patient_fk}__project_id": project.id})
        | Q(**{f"{voice_fk}__patient__project_id": project.id})
        | Q(**{f"processing_job__{patient_fk}__project_id": project.id})
    ).distinct()


def annotation_sets_of(project):
    from annotations.models import AnnotationSet

    patient_fk, _ = fk_fields_for(project.domain)
    return AnnotationSet.objects.filter(**{f"{patient_fk}__project_id": project.id})


def deletion_summary(project):
    """Counts of what deleting ``project`` destroys, for the confirmation page.

    Read-only, and each count is a real query -- nothing here is an estimate.
    """
    from annotations.models import AnnotationPayload, AnnotationRevision, SourceResource

    sets = annotation_sets_of(project)
    revisions = AnnotationRevision.objects.filter(annotation_set__in=sets)
    file_ids = files_of(project).values("id")

    return {
        "folders": folders_of(project).count(),
        "patients": patients_of(project).count(),
        "files": files_of(project).count(),
        "annotation_sets": sets.count(),
        "annotation_revisions": revisions.count(),
        "annotation_items": sum(
            model.objects.filter(revision__in=revisions).count()
            for model in _item_models()
        ),
        "annotation_payloads": AnnotationPayload.objects.filter(
            revision__in=revisions
        ).count(),
        "source_resources": SourceResource.objects.filter(file_id__in=file_ids).count(),
    }


def delete_project(project):
    """Delete ``project`` and everything below it. Returns the summary counts.

    Irreversible. The caller is responsible for having asked.
    """
    from annotations.models import AnnotationRevision, SourceResource

    summary = deletion_summary(project)
    keys = _storage_keys(project)

    with transaction.atomic():
        sets = annotation_sets_of(project)
        # The items first, and not because the set's cascade would miss them --
        # each item is CASCADE-ed by its revision. ``AnnotationItemBase.target``
        # and ``.selector`` are PROTECT, and PROTECT raises while the protecting
        # row is still there, even when the very same delete is about to take
        # it. Deleting the set therefore raised ProtectedError on its own
        # targets for any project whose patients have real geometry on them,
        # which is every annotated project. Removing the items by revision
        # releases those holds, and the set's cascade then takes the revisions,
        # targets, selectors and payloads.
        revisions = AnnotationRevision.objects.filter(annotation_set__in=sets)
        for model in _item_models():
            model.objects.filter(revision__in=revisions).delete()
        sets.delete()
        # The targets are gone with the sets, so the resources they PROTECTed
        # can go, and with them the last hold on the files.
        SourceResource.objects.filter(file_id__in=files_of(project).values("id")).delete()
        project.delete()

    _sweep_storage(keys)
    logger.info(
        "Deleted project %s (%s) and its contents: %s", project.pk, project.slug, summary
    )
    return summary


def _storage_keys(project):
    """The stored ``file_path`` of every file the delete will remove.

    A row's ``file_path`` is either an object key or -- for folder uploads, which
    register one row per bundle -- the *prefix* the members live under. Both are
    swept by listing the prefix, so this needs no special case and no reading of
    ``metadata['files']``.
    """
    return [path for path in files_of(project).values_list("file_path", flat=True) if path]


def _sweep_storage(keys):
    """Best-effort removal of the objects behind deleted rows.

    Never raises: the rows are already gone, and an unreachable object store
    must not turn a completed delete into a 500. Anything left behind is logged
    with its key so it can be swept by hand.
    """
    if not keys:
        return
    try:
        from common.object_storage import get_object_storage

        storage = get_object_storage()
    except Exception:
        logger.warning("Object storage unavailable; %d key(s) left behind", len(keys))
        return

    for prefix in keys:
        try:
            for key in list(storage.list_keys(prefix)):
                storage.delete(key)
        except Exception:
            logger.warning("Unable to delete object(s) under %s", prefix, exc_info=True)


class FolderNotEmpty(Exception):
    """A folder still holding patients was asked to be deleted unforced.

    Carries the count so the caller can say how many, which is the only thing
    that makes the message actionable.
    """

    def __init__(self, patient_count):
        self.patient_count = patient_count
        super().__init__(
            f"Folder still contains {patient_count} patient(s). Move or delete them "
            "first, or confirm to delete the folder anyway."
        )


def folder_subtree(folder):
    """``folder`` and every folder beneath it, as a list of ids.

    ``Folder.parent`` is CASCADE, so deleting a folder takes its descendants
    with it -- and therefore the "is it empty?" question is about the whole
    subtree, not the one row. Folders are created flat by the app today; nested
    ones predate that and still exist.
    """
    ids = [folder.id]
    frontier = [folder.id]
    model = type(folder)
    while frontier:
        frontier = list(
            model.objects.filter(parent_id__in=frontier).values_list("id", flat=True)
        )
        ids.extend(frontier)
    return ids


def patients_in_folder(folder):
    """The patients that would be orphaned by deleting ``folder``.

    Counted with ``all_objects`` where it exists: a soft-deleted patient is
    still a row whose ``folder`` the cascade will null out, and it is still
    restorable, so it counts as content.
    """
    ids = folder_subtree(folder)
    Patient = apps.get_model(folder._meta.app_label, "Patient")
    manager = getattr(Patient, "all_objects", None) or Patient._base_manager
    return manager.filter(folder_id__in=ids)


def delete_folder(folder, *, force=False):
    """Delete ``folder`` and its sub-folders. Returns the patients set adrift.

    Patients are never deleted here -- ``Patient.folder`` is ``SET_NULL``, so
    they stay in their project and simply stop being filed. That is why this
    needs no annotation handling and no storage sweep: nothing that owns bytes
    is going away.

    Raises :class:`FolderNotEmpty` unless ``force``, so "the folder still has
    things in it" is a decision the caller has to make rather than a surprise.
    """
    count = patients_in_folder(folder).count()
    if count and not force:
        raise FolderNotEmpty(count)
    with transaction.atomic():
        folder.delete()
    logger.info("Deleted folder %s and its sub-folders; %d patient(s) unfiled", folder.pk, count)
    return count
