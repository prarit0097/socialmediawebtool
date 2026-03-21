from django.core.management.base import BaseCommand

from scheduler.services.publishing import publish_due_targets


class Command(BaseCommand):
    help = "Publish any posts that are due right now."

    def handle(self, *args, **options):
        result = publish_due_targets()
        self.stdout.write(
            self.style.SUCCESS(
                f"Posting run completed at {result['checked_at']}. Success={result['success']} Failed={result['failed']}"
            )
        )
