"""Create the LLM prompts this platform ships with.

Same contract as the other seed commands: creates what is missing, leaves edits alone,
``--force`` resets. The prompt is the part an admin is *meant* to tune -- the clinical
framing, the emphasis, the wording that suits their specialty -- so a deploy quietly
reverting it would be the worst kind of surprise.

Only the instruction is stored here. The output contract the parser depends on lives in
``common/llm_tasks.py`` and is appended by the code at call time, so no admin edit can
break the format for every domain at once.
"""

from django.core.management.base import BaseCommand

from common.llm_tasks import (
    CAPTION_TO_TEMPLATE,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_USER_TEMPLATE,
)
from common.models import PromptTemplate


SEEDS = (
    {
        "slug": CAPTION_TO_TEMPLATE.prompt_slug,
        "name": "Dictated caption into the report template",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_template": DEFAULT_USER_TEMPLATE,
        "notes": (
            "Used by the Structure button under voice captions. The output format is "
            "appended by the code and is not editable here."
        ),
    },
)


class Command(BaseCommand):
    help = "Create the shipped LLM prompts if they are missing."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force", action="store_true",
            help="Reset existing prompts to the shipped wording, discarding edits.",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Report and write nothing.",
        )

    def handle(self, *args, **options):
        for seed in SEEDS:
            existing = PromptTemplate.objects.filter(slug=seed["slug"]).first()

            if existing is None:
                if options["dry_run"]:
                    self.stdout.write(f"would create {seed['slug']}")
                    continue
                PromptTemplate.objects.create(**seed)
                self.stdout.write(self.style.SUCCESS(f"created {seed['slug']}"))
                continue

            if not options["force"]:
                self.stdout.write(
                    f"kept {seed['slug']} (v{existing.version}; --force to reset)"
                )
                continue
            if options["dry_run"]:
                self.stdout.write(f"would reset {seed['slug']}")
                continue

            for field, value in seed.items():
                setattr(existing, field, value)
            # save() bumps the version whenever the wording actually changed, so a report
            # produced before this reset stays attributable to the prompt that made it.
            existing.save()
            self.stdout.write(self.style.WARNING(
                f"reset {seed['slug']} to the shipped wording (now v{existing.version})"
            ))
