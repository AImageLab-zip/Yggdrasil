"""Create the external-service rows this platform knows how to use.

Idempotent, like ``setup_*_modalities``: run it after upgrading, run it twice, run it on
a database that already has hand-edited rows. It creates what is missing and leaves what
exists alone -- an admin who retuned a threshold or repointed a URL must not have that
overwritten by a deploy. ``--force`` is the explicit opt-in to reset a row to the
shipped defaults.

Rows are created *enabled but keyless*: the API key lives in the environment under the
name the row carries, so until that variable is set the service resolves to ``None`` and
the feature fails closed. That is the intended half-configured state -- visible in the
admin, harmless in production.
"""

from django.core.management.base import BaseCommand

from common import external_services
from common.external_config import LLM_SERVICE_SLUG, WHISPER_SERVICE_SLUG
from common.models import ExternalService


#: What each shipped service looks like out of the box. Every value here is also a
#: sensible thing for an admin to change afterwards, which is the point of the table.
SEEDS = (
    {
        "slug": WHISPER_SERVICE_SLUG,
        "name": "Live dictation (Whisper)",
        "kind": external_services.STT_WEBSOCKET.key,
        # The upstream this platform has always used. A deployment running its own
        # speech-to-text service points this at it -- "ws://<host>:9097/ws" over an
        # internal network needs no certificate, and ca_cert_path can then be blank.
        "base_url": "wss://155.185.48.254:9097/ws",
        "ca_cert_path": "/app/certs/whisper-live.pem",
        "api_key_env": "WHISPER_API_TOKEN",
        "model_name": "base",
        "parameters": {
            "beam_size": 1,
            "vad_threshold": 0.5,
            "min_segment_seconds": 2.0,
            "max_segment_seconds": 12.0,
            "compute_type": "int8",
        },
        "languages": ["it", "en", "es", "fr", "de"],
        "timeout_seconds": 30,
        "connect_timeout_seconds": 10,
        "notes": (
            "Base URL, language set and timeouts take effect on the next connection; "
            "model and compute type are read by the speech-to-text service itself at "
            "startup, so changing them here needs that service restarted. Leave "
            "ca_cert_path blank for a ws:// upstream; a wss:// one pins the "
            "certificate named here and never falls back to the system trust store."
        ),
    },
    {
        "slug": LLM_SERVICE_SLUG,
        "name": "Report structuring (OpenRouter)",
        "kind": external_services.LLM_CHAT_OPENAI.key,
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "model_name": "nvidia/nemotron-3-super-120b-a12b:free",
        "parameters": {"temperature": 0.2, "max_tokens": 4000,
                       "reasoning_effort": "low"},
        "languages": [],
        "timeout_seconds": 90,
        "connect_timeout_seconds": 10,
        "notes": (
            "Any OpenAI-compatible endpoint. Point base_url at a self-hosted vLLM or "
            "Ollama to keep dictated text inside the deployment. On a reasoning model "
            "the thinking is charged against max_tokens and then discarded, so keep the "
            "budget well above the report's length and the effort low."
        ),
    },
)


class Command(BaseCommand):
    help = "Create the external-service rows (speech-to-text, LLM) if they are missing."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force", action="store_true",
            help="Reset existing rows to the shipped defaults, discarding admin edits.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would change and write nothing.",
        )

    def handle(self, *args, **options):
        force = options["force"]
        dry_run = options["dry_run"]

        for seed in SEEDS:
            slug = seed["slug"]
            existing = ExternalService.objects.filter(slug=slug).first()

            if existing is None:
                if dry_run:
                    self.stdout.write(f"would create {slug}")
                    continue
                ExternalService.objects.create(**seed)
                self.stdout.write(self.style.SUCCESS(f"created {slug}"))
                continue

            if not force:
                self.stdout.write(f"kept {slug} (already present; --force to reset)")
                continue

            if dry_run:
                self.stdout.write(f"would reset {slug} to defaults")
                continue
            for field, value in seed.items():
                setattr(existing, field, value)
            existing.save()
            self.stdout.write(self.style.WARNING(f"reset {slug} to defaults"))

        self.stdout.write(
            "Keys are read from the environment by the name each row carries; "
            "a row whose variable is empty resolves to nothing and the feature fails closed."
        )
