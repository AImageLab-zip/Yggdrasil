# Laparoscopy Management Commands

Run commands in the web container after exporting the matching suffix:

```bash
export DOCKER_SUFFIX=...
docker exec -it toothfairy4m-web-$DOCKER_SUFFIX python manage.py <command>
```

## setup_laparoscopy_modalities

Creates the `laparoscopy` project and registers the `video` modality. Safe to re-run.

```bash
python manage.py setup_laparoscopy_modalities
```

## setup_laparoscopy_region_types

Creates or updates the standard laparoscopy region annotation classes and their colors. Safe to re-run.

```bash
python manage.py setup_laparoscopy_region_types
```

## import_laparoscopy_example_video

Imports a local `.mp4` or `.avi` as a predictable example patient and creates the same pending `video` job used by normal uploads.

```bash
python manage.py import_laparoscopy_example_video /path/to/example.mp4
```

Useful options:

```bash
python manage.py import_laparoscopy_example_video /path/to/example.mp4 \
  --name "Example Guided Tour" \
  --folder Tutorial \
  --dataset Tutorial \
  --visibility debug \
  --username admin \
  --overwrite
```

The command uploads the raw video to object storage, creates a `video_raw` `FileRegistry`, and enqueues a pending `Job` with the `laparoscopy_video_v1` output profile.
