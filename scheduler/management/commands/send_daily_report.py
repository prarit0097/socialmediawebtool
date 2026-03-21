from datetime import datetime

from django.core.management.base import BaseCommand

from scheduler.services.telegram import send_daily_report


class Command(BaseCommand):
    help = "Send the previous day's Telegram report."

    def add_arguments(self, parser):
        parser.add_argument("--date", type=str, help="Report date in YYYY-MM-DD format.")
        parser.add_argument("--force", action="store_true", help="Resend even if already sent.")

    def handle(self, *args, **options):
        report_date = None
        if options.get("date"):
            report_date = datetime.strptime(options["date"], "%Y-%m-%d").date()
        result = send_daily_report(force=options.get("force", False), report_date=report_date)
        self.stdout.write(self.style.SUCCESS(result["status_message"]))
