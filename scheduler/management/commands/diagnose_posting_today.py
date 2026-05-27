from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone

from scheduler.models import PostLog, PublishingTarget, ScheduledPostRun
from scheduler.services.health import build_target_health
from scheduler.services.publishing import _active_platforms, _slot_is_complete, get_daily_slots


class Command(BaseCommand):
    help = "Read-only diagnosis for why today's scheduled posts did or did not publish."

    def add_arguments(self, parser):
        parser.add_argument("--target-id", type=int, help="Limit diagnosis to one publishing target.")

    def _safe_write(self, message: str = "") -> None:
        self.stdout.write(message.encode("ascii", "backslashreplace").decode("ascii"))

    def _slot_status(self, target, slot, active_platforms, now):
        run = target.scheduled_runs.filter(scheduled_for=slot).first()
        if run:
            return run.status
        if _slot_is_complete(target, slot, active_platforms):
            return "done"
        if slot > now:
            return "upcoming"
        age_minutes = int((now - slot).total_seconds() // 60)
        if age_minutes <= settings.SCHEDULER_CATCHUP_MINUTES:
            return "due-window"
        if age_minutes <= settings.SCHEDULER_BACKLOG_DAYS * 24 * 60:
            return "backlog-pending"
        return "missed-outside-backlog"

    def handle(self, *args, **options):
        now = timezone.localtime()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)

        targets = PublishingTarget.objects.filter(is_active=True).select_related(
            "credential",
            "facebook_account",
            "instagram_account",
        )
        if options.get("target_id"):
            targets = targets.filter(pk=options["target_id"])

        self._safe_write(f"Posting diagnosis at {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        self._safe_write(f"Catchup window: {settings.SCHEDULER_CATCHUP_MINUTES} minutes")
        self._safe_write(f"Backlog window: {settings.SCHEDULER_BACKLOG_DAYS} day(s)")

        today_logs = PostLog.objects.filter(created_at__gte=start, created_at__lt=end)
        today_runs = ScheduledPostRun.objects.filter(scheduled_for__gte=start, scheduled_for__lt=end)
        summary = today_logs.values("status", "platform").annotate(count=Count("id")).order_by("status", "platform")
        if summary:
            self._safe_write("Today log summary:")
            for row in summary:
                self._safe_write(f"  {row['platform']} {row['status']}: {row['count']}")
        else:
            self._safe_write("Today log summary: no PostLog rows created today.")
        run_summary = today_runs.values("status").annotate(count=Count("id")).order_by("status")
        if run_summary:
            self._safe_write("Today scheduled-run summary:")
            for row in run_summary:
                self._safe_write(f"  {row['status']}: {row['count']}")
        else:
            self._safe_write("Today scheduled-run summary: no ScheduledPostRun rows created today.")

        if not targets.exists():
            self._safe_write("No matching active targets.")
            return

        for target in targets.order_by("display_name"):
            health = build_target_health(target)
            active_platforms = set(_active_platforms(target))
            slots = get_daily_slots(target, now)
            slot_statuses = [(slot, self._slot_status(target, slot, active_platforms, now)) for slot in slots]
            target_logs = list(
                today_logs.filter(target=target)
                .order_by("-created_at")
                .values("platform", "status", "drive_file_name", "message", "created_at", "published_at")[:6]
            )
            target_runs = list(
                today_runs.filter(target=target)
                .order_by("scheduled_for")
                .values("scheduled_for", "status", "drive_file_name", "last_error")[:6]
            )

            self._safe_write("")
            self._safe_write(f"[{target.pk}] {target.display_name}")
            self._safe_write(f"  token: {target.credential.label}")
            self._safe_write(f"  platforms: {', '.join(sorted(active_platforms)) or 'none'}")
            self._safe_write(f"  drive: {target.drive_folder_id or 'not set'} | media={health['media_count']} cached={health['cached_asset_count']}")

            if slot_statuses:
                slots_text = ", ".join(
                    f"{timezone.localtime(slot).strftime('%H:%M')}={status}"
                    for slot, status in slot_statuses
                )
                self._safe_write(f"  slots_today: {slots_text}")

            if health["current_file"]:
                self._safe_write(
                    "  current_file: "
                    f"{health['current_file'].get('name', 'unknown')} "
                    f"({health['current_file'].get('mimeType', 'unknown')})"
                )
            if health["pending_platforms"]:
                self._safe_write(f"  pending_platforms: {', '.join(health['pending_platforms'])}")
            if health["backoff_messages"]:
                for message in health["backoff_messages"]:
                    self._safe_write(f"  blocker: {message}")
            if target_runs:
                self._safe_write("  scheduled_runs:")
                for run in target_runs:
                    when = timezone.localtime(run["scheduled_for"]).strftime("%H:%M")
                    reason = (run["last_error"] or "").replace("\n", " ")[:180]
                    self._safe_write(
                        f"    {when} {run['status']} {run['drive_file_name'] or '-'}"
                        f"{' :: ' + reason if reason else ''}"
                    )
            if health["content_exhausted"]:
                self._safe_write("  blocker: content exhausted; add new Drive media files.")
            if not target.drive_folder_id:
                self._safe_write("  blocker: Drive folder not configured.")
            if not active_platforms:
                self._safe_write("  blocker: no active Facebook/Instagram account linked.")
            if not target_logs and not any(status == "due-window" for _, status in slot_statuses):
                self._safe_write("  today_activity: no due slot currently inside catchup window.")
            elif not target_logs:
                self._safe_write("  today_activity: due slot exists, but no PostLog was created today.")
            else:
                self._safe_write("  today_activity:")
                for log in target_logs:
                    when = timezone.localtime(log["published_at"] or log["created_at"]).strftime("%H:%M:%S")
                    reason = (log["message"] or "").replace("\n", " ")[:180]
                    self._safe_write(
                        f"    {when} {log['platform']} {log['status']} "
                        f"{log['drive_file_name'] or '-'} :: {reason or '-'}"
                    )

            issues = [issue for issue in health["issues"] if issue]
            if issues:
                for issue in issues[:6]:
                    self._safe_write(f"  issue: {issue}")
