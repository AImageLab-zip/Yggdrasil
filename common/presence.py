"""Live user presence tracking, backed by Redis.

Each authenticated request refreshes a short-lived key for that user.
A key existing means the user is "online"; expiry means they dropped off.
No cleanup job is needed since Redis handles eviction via TTL.
"""

import json
import logging

import redis
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

_KEY_PREFIX = "presence:user:"
_SYNC_KEY_PREFIX = "presence:dbsync:"
# How often (seconds) a given user's session row is updated in the DB.
# Bounds write volume while keeping the timeline accurate to within this window.
_SESSION_SYNC_INTERVAL_SECONDS = 30

_PROJECT_SLUGS = {"maxillo", "brain", "laparoscopy"}

_client = None


def project_slug_from_path(path):
    """Mirrors ProjectSessionMiddleware's URL-prefix convention."""
    parts = [p for p in path.split("/") if p]
    if parts and parts[0] in _PROJECT_SLUGS:
        return parts[0]
    return ""


def get_redis_client():
    global _client
    if _client is None:
        _client = redis.Redis.from_url(settings.REDIS_PRESENCE_URL, decode_responses=True)
    return _client


def touch(user, path):
    """Record that `user` is active, refreshing their TTL key and (rate-limited) DB session."""
    project_slug = project_slug_from_path(path)
    try:
        payload = json.dumps({
            "user_id": user.id,
            "username": user.username,
            "full_name": user.get_full_name() or user.username,
            "path": path,
            "project_slug": project_slug,
        })
        client = get_redis_client()
        client.set(f"{_KEY_PREFIX}{user.id}", payload, ex=settings.PRESENCE_TTL_SECONDS)

        # Rate-limit DB writes: only sync the session row once per
        # _SESSION_SYNC_INTERVAL_SECONDS per (user, project), set with NX so
        # concurrent requests don't all win the race and double-sync.
        sync_key = f"{_SYNC_KEY_PREFIX}{user.id}:{project_slug}"
        if client.set(sync_key, "1", ex=_SESSION_SYNC_INTERVAL_SECONDS, nx=True):
            _sync_session(user, project_slug)
    except redis.RedisError:
        logger.warning("Could not record presence for user %s", user.id, exc_info=True)


def _sync_session(user, project_slug):
    """Extend the user's current per-project session, or start a new one if
    their last heartbeat for that project is older than the presence TTL
    (i.e. they had gone offline, or switched to a different project)."""
    from .models import UserSession

    now = timezone.now()
    grace = settings.PRESENCE_TTL_SECONDS + _SESSION_SYNC_INTERVAL_SECONDS
    last_session = (
        UserSession.objects.filter(user=user, project_slug=project_slug)
        .order_by("-last_seen_at")
        .first()
    )

    if last_session and (now - last_session.last_seen_at).total_seconds() <= grace:
        last_session.last_seen_at = now
        last_session.save(update_fields=["last_seen_at"])
    else:
        UserSession.objects.create(
            user=user, project_slug=project_slug, started_at=now, last_seen_at=now
        )


def get_online_users():
    """Return presence payloads for all currently online users, newest TTL first."""
    client = get_redis_client()
    try:
        keys = list(client.scan_iter(match=f"{_KEY_PREFIX}*", count=200))
        if not keys:
            return []
        values = client.mget(keys)
        ttls = client.pipeline()
        for key in keys:
            ttls.ttl(key)
        ttl_values = ttls.execute()
    except redis.RedisError:
        logger.warning("Could not fetch online users", exc_info=True)
        return []

    users = []
    for value, ttl in zip(values, ttl_values):
        if not value:
            continue
        try:
            data = json.loads(value)
        except (TypeError, ValueError):
            continue
        data["ttl_seconds"] = ttl if ttl and ttl > 0 else 0
        users.append(data)

    users.sort(key=lambda u: u["full_name"].lower())
    return users
