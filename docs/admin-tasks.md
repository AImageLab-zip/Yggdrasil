# Admin tasks

One-off operational scripts in the repo root. All of them depend on `DOCKER_SUFFIX` being exported in your shell (matching the value in `.env`).

## Create a superuser

```bash
export DOCKER_SUFFIX=dev-yourname
docker exec -it toothfairy4m-web-$DOCKER_SUFFIX python manage.py createsuperuser
```

This is also what [set_llumetti_as_admin.sh](../set_llumetti_as_admin.sh) does — run it directly, or copy the pattern to promote a different user.

## Import a production DB dump

[import_prod_db.sh](../import_prod_db.sh) imports a subset of tables (`auth_user`, `common_projectaccess`, `common_project`, `common_modality`) from a `mysqldump` SQL file into the running `db` container, without touching the rest of the schema/data.

```bash
./import_prod_db.sh path/to/dump.sql
```

It reads `.env` from the repo root for DB credentials, and resolves the target container as `toothfairy4m-db-$DOCKER_SUFFIX`.

This is destructive to the listed tables (`DROP TABLE IF EXISTS` is replayed from the dump) — back up first if unsure.

> This is a **subset** import for seeding users/projects/modalities into a dev stack. It is **not** how you migrate a server. For a full v1.9 → 2.0 server migration (whole database restore + additive migrations + object storage), use `scripts/restore_prod.sh` and follow [upgrade-1.9-to-2.0.md](upgrade-1.9-to-2.0.md).
