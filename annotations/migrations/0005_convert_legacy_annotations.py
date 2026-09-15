"""Convert the legacy per-domain annotation tables as part of ``migrate``.

**Why this is a migration and not only a command.** Upgrading a 1.9 or 2.0 deployment to
3.0 has to leave a working system without anybody remembering a second step. It did not:
the strips of 54 browser-generated panoramics sat in object storage while
``patient_panoramic_data`` -- which reads the arch through ``annotations`` -- answered 404,
because the conversion had never been run on that deployment. "The PNG is right there and
the viewer says it is not available" is what an un-run conversion looks like from the
outside, and no amount of care in the reader can distinguish it from a study that really
has no panoramic.

``annotations_convert_legacy``'s own header argues against a ``RunPython``, and those
arguments are real -- unbounded row counts, no resume after a failure halfway, a blocked
deploy. They are answered rather than ignored:

* **Unbounded and slow.** The command works patient by patient in its own transaction, so
  this migration is not one long lock; and it converts rows that a 3.0 deployment cannot
  read at all until it has, which is not work that can be deferred past the upgrade.
* **Not resumable.** It is idempotent by construction -- a surface that already has a set
  with at least one revision is skipped -- so a re-run *is* the resume, whether it comes
  from a re-``migrate`` or from running the command by hand.
* **Blocking the deploy.** ``continue_on_error`` is on, so one unexpected legacy shape
  logs and the rest convert. A migration that refuses to finish because of a single odd
  row would strand the upgrade in a worse place than a smaller conversion does. The
  summary names the command to re-run once the row is understood.

The command is invoked rather than reimplemented against historical models. That is the
usual data-migration caveat -- it reads *current* model classes -- and it is the right
trade here: this is the last migration in the graph for these tables, and a second
implementation of the conversion is a second thing to keep correct, on exactly the code
path where a mistake silently loses somebody's annotations.
"""

from django.core.management import call_command
from django.db import migrations


def convert(apps, schema_editor):
    # Nothing to convert on a database that has no legacy rows -- a fresh install, or a
    # deployment where this already ran. The command reports `converted 0` in both cases
    # and writes nothing, so it is cheap to reach here.
    call_command(
        "annotations_convert_legacy",
        continue_on_error=True,
        verbosity=1,
    )


def unconvert(apps, schema_editor):
    """Deliberately a no-op.

    The legacy tables are left untouched by the forward pass -- the conversion copies,
    it does not move -- so rolling this migration back needs to undo nothing. Deleting
    the converted sets would destroy revisions that a 3.0 deployment may have added on
    top of them, which is not a reversal.
    """


class Migration(migrations.Migration):
    # Every legacy table this reads must be at its final shape, and the annotations
    # schema must be complete: `0003` adds `AnnotationTarget.status`, which the
    # intraoral conversion writes per image, and `0004` widens `EventAnnotationItem.value`
    # to hold a real voice-caption transcript.
    dependencies = [
        ("annotations", "0004_event_value_text"),
        ("brain", "0021_patient_project_required"),
        ("common", "0049_backfill_modality_domain"),
        ("laparoscopy", "0013_patient_project_required"),
        ("maxillo", "0029_patient_project_required"),
    ]

    operations = [
        migrations.RunPython(convert, unconvert, elidable=False),
    ]
