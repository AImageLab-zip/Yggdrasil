"""Settings for running the Django suite off a throwaway SQLite file.

Not used by any deployment. `yggdrasil/settings.py` hard-wires MySQL and validates its
credentials at import, so a test run otherwise needs the production database server for
a suite that only ever touches a `test_` database.
"""

import os

# settings.py refuses to import without these (it validates production config at
# import time). Test-only placeholders -- setdefault, so a real environment wins and a
# CI env block keeps working. Nothing here is used: the database is SQLite below, mail
# goes to the locmem backend, and no broker is contacted by the suite.
for _name, _value in {
    "SECRET_KEY": "sqlite-test-only-not-a-secret",
    "DB_NAME": "unused",
    "DB_USER": "unused",
    "DB_PASSWORD": "unused",
    "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
    "EMAIL_HOST": "localhost",
    "EMAIL_PORT": "25",
    "EMAIL_HOST_USER": "",
    "EMAIL_HOST_PASSWORD": "",
    "EMAIL_USE_TLS": "false",
    "EMAIL_USE_SSL": "false",
    "DEFAULT_FROM_EMAIL": "tests@localhost",
    "REDIS_PASSWORD": "unused",
}.items():
    os.environ.setdefault(_name, _value)

from .settings import *  # noqa: E402,F401,F403

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
