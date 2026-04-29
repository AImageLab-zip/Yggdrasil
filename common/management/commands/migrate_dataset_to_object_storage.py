import json
import os
from typing import Any, Dict, Iterable, List, Tuple

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "toothfairy.settings")

from common.models import FileRegistry, Job
from common.object_storage import ObjectStorageError, get_object_storage

MISSING = object()

def _norm_dataset_root(dataset_root: str) -> str:
    dataset_root = (dataset_root or "/dataset").rstrip("/")
    if not dataset_root.startswith("/"):
        dataset_root = "/" + dataset_root
    return dataset_root


def _local_path_to_key(local_path: str, *, dataset_root: str) -> str:
    local_path = (local_path or "").strip()
    if not local_path.startswith("/"):
        raise ValueError("Expected absolute local path")

    dataset_root = _norm_dataset_root(dataset_root)

    if local_path == dataset_root:
        rel = ""
    elif local_path.startswith(dataset_root + "/"):
        rel = local_path[len(dataset_root) + 1 :]
    else:
        rel = local_path.lstrip("/")

    rel = rel.replace(os.sep, "/").lstrip("/")
    parts = [p for p in rel.split("/") if p and p not in {".", ".."}]
    key = "/".join(parts)
    if ".." in key.split("/"):
        raise ValueError("Invalid relative key")
    return key


def _should_rewrite_path(value: str, *, dataset_root: str) -> bool:
    if not value or not isinstance(value, str):
        return False
    if not value.startswith("/"):
        return False

    dataset_root = _norm_dataset_root(dataset_root)
    return value == dataset_root or value.startswith(dataset_root + "/")


def _rewrite_any(obj: Any, *, dataset_root: str, ensure_uploaded) -> Tuple[Any, bool]:
    if isinstance(obj, str):
        if _should_rewrite_path(obj, dataset_root=dataset_root):
            key = ensure_uploaded(obj)
            if key is MISSING:
                return obj, False
            return key, True
        return obj, False

    if isinstance(obj, list):
        changed = False
        out = []
        for item in obj:
            new_item, ch = _rewrite_any(
                item,
                dataset_root=dataset_root,
                ensure_uploaded=ensure_uploaded,
            )
            out.append(new_item)
            changed = changed or ch
        return out, changed

    if isinstance(obj, dict):
        changed = False
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            new_v, ch = _rewrite_any(
                v,
                dataset_root=dataset_root,
                ensure_uploaded=ensure_uploaded,
            )
            out[k] = new_v
            changed = changed or ch
        return out, changed

    return obj, False


def _iter_dataset_paths(obj: Any, *, dataset_root: str):
    if isinstance(obj, str):
        if _should_rewrite_path(obj, dataset_root=dataset_root):
            yield obj
        return

    if isinstance(obj, list):
        for item in obj:
            yield from _iter_dataset_paths(item, dataset_root=dataset_root)
        return

    if isinstance(obj, dict):
        for value in obj.values():
            yield from _iter_dataset_paths(value, dataset_root=dataset_root)
        return


class Command(BaseCommand):
    help = (
        "Migrate legacy /dataset artifacts to object storage and rewrite DB references."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dataset-root",
            default=getattr(settings, "DATASET_PATH", "/dataset"),
            help="Root path that contains the legacy dataset (default: settings.DATASET_PATH).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Perform uploads and DB writes (default is dry-run).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Limit number of records per model (0 = no limit).",
        )
        parser.add_argument(
            "--fail-missing",
            action="store_true",
            help=(
                "Abort on missing filesystem paths (default: skip and report missing)."
            ),
        )

    def handle(self, *args, **options):
        dataset_root = _norm_dataset_root(options["dataset_root"])
        do_apply = bool(options["apply"])
        limit = int(options["limit"] or 0)
        fail_missing = bool(options.get("fail_missing"))

        self.stdout.write(f"Dataset root: {dataset_root}")
        self.stdout.write("Mode: APPLY" if do_apply else "Mode: DRY-RUN")

        storage = get_object_storage()
        uploaded: Dict[str, Any] = {}
        missing_entries: List[Tuple[str, str, str, str]] = []
        missing_seen = set()
        current_context = {"model": "", "id": "", "field": ""}

        def _iter_qs(qs: Iterable, *, limit_n: int):
            if limit_n and hasattr(qs, "all"):
                return qs.all()[:limit_n]
            return qs

        def _progress_bar(current: int, total: int, width: int = 30) -> str:
            if total <= 0:
                return f"{current} files"
            filled = int(width * current / total)
            bar = "#" * filled + "-" * (width - filled)
            pct = int(100 * current / total)
            return f"[{bar}] {pct:3d}% ({current}/{total})"

        def _count_files(root_path: str) -> int:
            total_files = 0
            for _, _, files in os.walk(root_path):
                total_files += len(files)
            return total_files

        def _collect_all_paths() -> Iterable[str]:
            fr_qs = (
                FileRegistry.objects.exclude(file_path="")
                .filter(file_path__startswith=dataset_root)
                .order_by("id")
            )
            for fr in _iter_qs(fr_qs.iterator(), limit_n=limit):
                yield from _iter_dataset_paths(fr.file_path, dataset_root=dataset_root)
                if isinstance(fr.metadata, (dict, list)):
                    yield from _iter_dataset_paths(
                        fr.metadata, dataset_root=dataset_root
                    )

            job_qs = (
                Job.objects.exclude(input_file_path="")
                .filter(input_file_path__startswith=dataset_root)
                .order_by("id")
            )
            for job in _iter_qs(job_qs.iterator(), limit_n=limit):
                original = job.input_file_path or ""
                s = original.strip()
                if s.startswith("{") or s.startswith("["):
                    try:
                        parsed = json.loads(original)
                        yield from _iter_dataset_paths(
                            parsed, dataset_root=dataset_root
                        )
                    except Exception:
                        if _should_rewrite_path(original, dataset_root=dataset_root):
                            yield original
                else:
                    if _should_rewrite_path(original, dataset_root=dataset_root):
                        yield original

                if isinstance(job.output_files, (dict, list)):
                    yield from _iter_dataset_paths(
                        job.output_files, dataset_root=dataset_root
                    )

            for export_model_path in ["maxillo.models.Export", "brain.models.Export"]:
                try:
                    module_name, model_name = export_model_path.rsplit(".", 1)
                    module = __import__(module_name, fromlist=[model_name])
                    ExportModel = getattr(module, model_name)

                    export_qs = (
                        ExportModel.objects.exclude(file_path="")
                        .filter(file_path__startswith=dataset_root)
                        .order_by("id")
                    )
                    for ex in _iter_qs(export_qs.iterator(), limit_n=limit):
                        yield from _iter_dataset_paths(
                            ex.file_path, dataset_root=dataset_root
                        )
                except Exception:
                    continue

        progress = None
        if do_apply:
            try:
                from tqdm import tqdm
            except ImportError as exc:
                raise CommandError(
                    "tqdm is required for the progress bar. Install it with 'pip install tqdm'"
                ) from exc

            unique_paths = set(p for p in _collect_all_paths() if p)
            total_files = 0
            counted: Dict[str, int] = {}
            for local_path in unique_paths:
                if local_path in counted:
                    total_files += counted[local_path]
                    continue
                if not os.path.exists(local_path):
                    counted[local_path] = 0
                    continue
                if os.path.isdir(local_path):
                    counted[local_path] = _count_files(local_path)
                else:
                    counted[local_path] = 1
                total_files += counted[local_path]

            progress = tqdm(
                total=total_files,
                unit="file",
                ascii=True,
                ncols=80,
            )

        def _set_context(model: str, obj_id: str, field: str) -> None:
            current_context["model"] = model
            current_context["id"] = str(obj_id)
            current_context["field"] = field

        def _record_missing(local_path: str) -> None:
            entry = (
                local_path,
                current_context["model"],
                current_context["id"],
                current_context["field"],
            )
            if entry in missing_seen:
                return
            missing_seen.add(entry)
            missing_entries.append(entry)

        def ensure_uploaded(local_path: str):
            local_path = (local_path or "").strip()
            if local_path in uploaded:
                return uploaded[local_path]

            key = _local_path_to_key(local_path, dataset_root=dataset_root)

            if not do_apply:
                uploaded[local_path] = key
                return key

            if not os.path.exists(local_path):
                if fail_missing:
                    raise CommandError(f"Missing path: {local_path}")
                _record_missing(local_path)
                uploaded[local_path] = MISSING
                return MISSING

            if os.path.isdir(local_path):
                prefix_key = key.rstrip("/")
                total_files = _count_files(local_path)
                uploaded_files = 0
                progress_every = max(1, total_files // 100) if total_files else 1
                if total_files and not progress:
                    self.stdout.write(
                        f"Uploading directory {local_path} ({total_files} files)"
                    )
                for root, _, files in os.walk(local_path):
                    for fname in files:
                        src = os.path.join(root, fname)
                        child_key = _local_path_to_key(src, dataset_root=dataset_root)
                        storage.upload_file(src, key=child_key)
                        uploaded_files += 1
                        if progress:
                            progress.update(1)
                        elif total_files and (
                            uploaded_files % progress_every == 0
                            or uploaded_files == total_files
                        ):
                            self.stdout.write(
                                _progress_bar(uploaded_files, total_files),
                                ending="\r",
                            )
                if total_files and not progress:
                    self.stdout.write("", ending="\n")
                uploaded[local_path] = prefix_key
                return prefix_key

            storage.upload_file(local_path, key=key)
            if progress:
                progress.update(1)
            uploaded[local_path] = key
            return key
        try:
            fr_qs = (
                FileRegistry.objects.exclude(file_path="")
                .filter(file_path__startswith=dataset_root)
                .order_by("id")
            )
            fr_count = fr_qs.count()
            self.stdout.write(f"FileRegistry candidates: {fr_count}")

            fr_updated = 0
            for fr in _iter_qs(fr_qs.iterator(), limit_n=limit):
                try:
                    _set_context("FileRegistry", fr.id, "file_path")
                    new_path = ensure_uploaded(fr.file_path)
                    if new_path is MISSING:
                        continue
                    changed = new_path != fr.file_path

                    new_metadata = fr.metadata
                    meta_changed = False
                    if isinstance(fr.metadata, (dict, list)):
                        _set_context("FileRegistry", fr.id, "metadata")
                        new_metadata, meta_changed = _rewrite_any(
                            fr.metadata,
                            dataset_root=dataset_root,
                            ensure_uploaded=ensure_uploaded,
                        )

                    if changed or meta_changed:
                        fr_updated += 1
                        if do_apply:
                            fr.file_path = new_path
                            if meta_changed:
                                fr.metadata = new_metadata
                            fr.save(
                                update_fields=["file_path", "metadata"]
                                if meta_changed
                                else ["file_path"]
                            )
                except (ObjectStorageError, OSError, ValueError) as e:
                    raise CommandError(f"FileRegistry {fr.id} failed: {e}")

            self.stdout.write(f"FileRegistry updated: {fr_updated}")

            job_qs = (
                Job.objects.exclude(input_file_path="")
                .filter(input_file_path__startswith=dataset_root)
                .order_by("id")
            )
            job_count = job_qs.count()
            self.stdout.write(f"Job candidates (input_file_path): {job_count}")

            job_updated = 0
            for job in _iter_qs(job_qs.iterator(), limit_n=limit):
                original = job.input_file_path or ""
                new_val = original
                changed = False

                s = original.strip()
                if s.startswith("{") or s.startswith("["):
                    try:
                        parsed = json.loads(original)
                        _set_context("Job", job.id, "input_file_path")
                        rewritten, ch = _rewrite_any(
                            parsed,
                            dataset_root=dataset_root,
                            ensure_uploaded=ensure_uploaded,
                        )
                        if ch:
                            new_val = json.dumps(rewritten)
                            changed = True
                    except Exception:
                        if _should_rewrite_path(original, dataset_root=dataset_root):
                            _set_context("Job", job.id, "input_file_path")
                            maybe_key = ensure_uploaded(original)
                            if maybe_key is not MISSING:
                                new_val = maybe_key
                                changed = True
                else:
                    if _should_rewrite_path(original, dataset_root=dataset_root):
                        _set_context("Job", job.id, "input_file_path")
                        maybe_key = ensure_uploaded(original)
                        if maybe_key is not MISSING:
                            new_val = maybe_key
                            changed = True

                out_changed = False
                new_outputs = job.output_files
                if isinstance(job.output_files, (dict, list)):
                    _set_context("Job", job.id, "output_files")
                    new_outputs, out_changed = _rewrite_any(
                        job.output_files,
                        dataset_root=dataset_root,
                        ensure_uploaded=ensure_uploaded,
                    )

                if changed or out_changed:
                    job_updated += 1
                    if do_apply:
                        job.input_file_path = new_val
                        if out_changed:
                            job.output_files = new_outputs
                        job.save(
                            update_fields=["input_file_path", "output_files"]
                            if out_changed
                            else ["input_file_path"]
                        )

            self.stdout.write(f"Jobs updated: {job_updated}")

            for export_model_path in ["maxillo.models.Export", "brain.models.Export"]:
                try:
                    module_name, model_name = export_model_path.rsplit(".", 1)
                    module = __import__(module_name, fromlist=[model_name])
                    ExportModel = getattr(module, model_name)

                    export_qs = (
                        ExportModel.objects.exclude(file_path="")
                        .filter(file_path__startswith=dataset_root)
                        .order_by("id")
                    )
                    export_count = export_qs.count()
                    self.stdout.write(f"{export_model_path} candidates: {export_count}")

                    export_updated = 0
                    for ex in _iter_qs(export_qs.iterator(), limit_n=limit):
                        _set_context(export_model_path, ex.id, "file_path")
                        new_path = ensure_uploaded(ex.file_path)
                        if new_path is MISSING:
                            continue
                        if new_path != ex.file_path:
                            export_updated += 1
                            if do_apply:
                                ex.file_path = new_path
                                ex.save(update_fields=["file_path"])

                    self.stdout.write(f"{export_model_path} updated: {export_updated}")
                except Exception as e:
                    self.stdout.write(f"Skipping {export_model_path} migration: {e}")

            if not do_apply:
                self.stdout.write(
                    "Dry-run complete. Re-run with --apply to perform uploads and DB updates."
                )
            if missing_entries:
                self.stdout.write(
                    f"Missing paths skipped: {len(missing_entries)}"
                )
                max_show = 50
                for local_path, model, obj_id, field in missing_entries[:max_show]:
                    self.stdout.write(
                        f"- {local_path} (from {model} {obj_id} {field})"
                    )
                if len(missing_entries) > max_show:
                    remaining = len(missing_entries) - max_show
                    self.stdout.write(
                        f"... and {remaining} more."
                    )
        finally:
            if progress:
                progress.close()
