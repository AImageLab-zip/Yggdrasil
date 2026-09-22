"""Logging filters applied to every handler (see ``LOGGING`` in settings)."""

import logging
import re

_SHARE_TOKEN = re.compile(r"(/export/shared/)[^/\s\"'?#]+")


class RedactShareTokens(logging.Filter):
    """Replace export share tokens in log lines with ``<redacted>``.

    A share token *is* the credential for a public export link, and request
    paths are logged at several levels (request middleware, ``django.request``
    4xx/5xx warnings, tracebacks), so any log reader could reuse a live link.
    """

    def filter(self, record):
        message = record.getMessage()
        redacted = _SHARE_TOKEN.sub(r"\1<redacted>", message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True
