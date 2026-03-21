import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from scheduler.services.publishing import publish_due_targets
from scheduler.services.telegram import send_daily_report


class Command(BaseCommand):
    help = "Continuously poll for due posts and send the daily Telegram report."

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS("Scheduler started. Press Ctrl+C to stop."))
        while True:
            now = timezone.localtime()
            publish_due_targets(reference_time=now)
            if now.hour == settings.REPORT_HOUR and now.minute == 0:
                try:
                    send_daily_report()
                except Exception as exc:
                    self.stderr.write(f"Daily report failed: {exc}")
            time.sleep(settings.SCHEDULER_POLL_SECONDS)
