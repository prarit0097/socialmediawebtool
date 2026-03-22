import atexit
import os
import time
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from scheduler.models import DailyReportLog
from scheduler.services.publishing import publish_due_targets
from scheduler.services.telegram import send_daily_report


LOCK_FILE = Path(settings.BASE_DIR) / ".run_scheduler.lock"


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _acquire_scheduler_lock() -> None:
    current_pid = os.getpid()
    if LOCK_FILE.exists():
        existing_text = LOCK_FILE.read_text(encoding="utf-8").strip()
        try:
            existing_pid = int(existing_text)
        except ValueError:
            existing_pid = 0
        if existing_pid and existing_pid != current_pid and _pid_is_running(existing_pid):
            raise RuntimeError(f"Another scheduler instance is already running with PID {existing_pid}.")
        LOCK_FILE.unlink(missing_ok=True)

    LOCK_FILE.write_text(str(current_pid), encoding="utf-8")

    def _cleanup():
        if LOCK_FILE.exists() and LOCK_FILE.read_text(encoding="utf-8").strip() == str(current_pid):
            LOCK_FILE.unlink(missing_ok=True)

    atexit.register(_cleanup)


def _should_send_daily_report(now) -> bool:
    if now.hour < settings.REPORT_HOUR:
        return False
    report_date = now.date() - timedelta(days=1)
    report_log = DailyReportLog.objects.filter(report_date=report_date).first()
    if not report_log or not report_log.sent_at:
        return True
    return timezone.localtime(report_log.sent_at).date() != now.date()


class Command(BaseCommand):
    help = "Continuously poll for due posts and send the daily Telegram report."

    def handle(self, *args, **options):
        try:
            _acquire_scheduler_lock()
        except RuntimeError as exc:
            self.stderr.write(str(exc))
            return

        self.stdout.write(self.style.SUCCESS("Scheduler started. Press Ctrl+C to stop."))
        while True:
            now = timezone.localtime()
            publish_due_targets(reference_time=now)
            if _should_send_daily_report(now):
                try:
                    send_daily_report()
                except Exception as exc:
                    self.stderr.write(f"Daily report failed: {exc}")
            time.sleep(settings.SCHEDULER_POLL_SECONDS)
