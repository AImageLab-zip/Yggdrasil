# Admin tasks

Common operational tasks. Commands depend on `DOCKER_SUFFIX` being exported in your shell (matching the value in `.env`).

## Create a superuser

```bash
export DOCKER_SUFFIX=dev-yourname
docker exec -it toothfairy4m-web-$DOCKER_SUFFIX python manage.py createsuperuser
```

To promote an existing user, use the Django admin (`/admin/`) or a `manage.py shell` one-liner:

```bash
docker exec -it toothfairy4m-web-$DOCKER_SUFFIX python manage.py shell -c \
  "from django.contrib.auth.models import User; u=User.objects.get(username='NAME'); u.is_staff=True; u.is_superuser=True; u.save()"
```

## Restore a production DB dump

For a full v1.9 → 2.0 server migration (whole database restore + additive migrations + object storage), use `scripts/restore_prod.sh` and follow [upgrade-1.9-to-2.0.md](upgrade-1.9-to-2.0.md).
