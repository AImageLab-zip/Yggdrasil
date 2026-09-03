"""Settings for running the Django suite off a throwaway SQLite file.

Not used by any deployment. `yggdrasil/settings.py` hard-wires MySQL and validates its
credentials at import, so a test run otherwise needs the production database server for
a suite that only ever touches a `test_` database.
"""

from .settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "OPTIONS": {},
        "TEST": {"NAME": None},
    }
}
SECURE_SSL_REDIRECT = False
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
