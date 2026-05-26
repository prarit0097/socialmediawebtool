from django.core.management.base import BaseCommand

from scheduler.services.publishing import publish_due_targets


class Command(BaseCommand):
    help = "Publish any posts that are due right now."

    def handle(self, *args, **options):
        result = publish_due_targets()
        self.stdout.write(
            self.style.SUCCESS(
                (
                    f"Posting run completed at {result['checked_at']}. "
                    f"Checked={result['checked_targets']} Success={result['success']} "
                    f"Failed={result['failed']} Skipped={result['skipped']} "
                    f"Backoff={result['backoff']} ContentExhausted={result['content_exhausted']}"
                )
            )
        )
