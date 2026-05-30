from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.utils import timezone

from scheduler.models import MetaCredential, PostLog, ScheduledPostRun, SocialAccount
from scheduler.services.publishing import _is_meta_rate_limit_error, _is_synthetic_backoff_message


class Command(BaseCommand):
    help = "Release Instagram runs blocked only by another target's credential-scoped publishing limit."

    def add_arguments(self, parser):
        parser.add_argument("--credential-id", type=int)
        parser.add_argument("--credential-label")
        parser.add_argument("--apply", action="store_true")

    def _target_file_has_active_rate_limit(self, run: ScheduledPostRun, platform: str, now) -> bool:
        if not run.drive_file_id:
            return False
        app_backoff = getattr(settings, "META_APP_RATE_LIMIT_BACKOFF_MINUTES", settings.META_RATE_LIMIT_BACKOFF_MINUTES)
        since = now - timedelta(minutes=app_backoff)
        logs = (
            PostLog.objects.filter(
                target=run.target,
                platform=platform,
                status=PostLog.STATUS_FAILED,
                drive_file_id=run.drive_file_id,
                created_at__gte=since,
            )
            .exclude(message="")
            .order_by("-created_at")
            .values_list("message", flat=True)[:10]
        )
        return any(_is_meta_rate_limit_error(message) and not _is_synthetic_backoff_message(message) for message in logs)

    def handle(self, *args, **options):
        credentials = MetaCredential.objects.all().order_by("id")
        if options["credential_id"]:
            credentials = credentials.filter(id=options["credential_id"])
        if options["credential_label"]:
            credentials = credentials.filter(label=options["credential_label"])

        now = timezone.now()
        apply_changes = options["apply"]
        checked = 0
        released = 0

        for credential in credentials:
            runs = ScheduledPostRun.objects.filter(
                target__credential=credential,
                status=ScheduledPostRun.STATUS_BACKOFF,
                last_error__icontains="credential/platform",
            ).select_related("target")
            for run in runs.order_by("target_id", "scheduled_for"):
                checked += 1
                statuses = dict(run.platform_status or {})
                if statuses.get(SocialAccount.INSTAGRAM) != ScheduledPostRun.STATUS_BACKOFF:
                    continue
                if self._target_file_has_active_rate_limit(run, SocialAccount.INSTAGRAM, now):
                    continue

                released += 1
                self.stdout.write(
                    "RELEASE "
                    f"run={run.id} target={run.target_id} slot={timezone.localtime(run.scheduled_for)} "
                    f"file={run.drive_file_name!r} old_retry={timezone.localtime(run.next_retry_at) if run.next_retry_at else None}"
                )
                if apply_changes:
                    statuses[SocialAccount.INSTAGRAM] = ScheduledPostRun.STATUS_PENDING
                    run.status = ScheduledPostRun.STATUS_PENDING
                    run.platform_status = statuses
                    run.next_retry_at = None
                    run.last_error = ""
                    run.save(update_fields=["status", "platform_status", "next_retry_at", "last_error", "updated_at"])
                    if "credential/platform" in (run.target.last_error or ""):
                        run.target.last_status = ""
                        run.target.last_error = ""
                        run.target.save(update_fields=["last_status", "last_error", "updated_at"])

        if apply_changes:
            cache.clear()
        self.stdout.write(f"checked={checked} released={released} applied={apply_changes}")
