from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.cache import cache
from django.utils import timezone

from scheduler.models import MetaCredential, PostLog, ScheduledPostRun


RATE_LIMIT_MARKERS = (
    "application request limit reached",
    "rate limit",
    "too many calls",
    "calls to this api have exceeded",
)
SYNTHETIC_PREFIXES = (
    "meta rate limit backoff active",
    "meta transient backoff active",
)


class Command(BaseCommand):
    help = "Clamp existing Meta backoff scheduled runs to the latest real rate-limit expiry."

    def add_arguments(self, parser):
        parser.add_argument("--credential-id", type=int)
        parser.add_argument("--credential-label")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        credentials = MetaCredential.objects.all().order_by("id")
        if options["credential_id"]:
            credentials = credentials.filter(id=options["credential_id"])
        if options["credential_label"]:
            credentials = credentials.filter(label=options["credential_label"])

        total = 0
        fixed = 0
        now = timezone.now()
        app_backoff = getattr(settings, "META_APP_RATE_LIMIT_BACKOFF_MINUTES", settings.META_RATE_LIMIT_BACKOFF_MINUTES)
        window_start = now - timedelta(minutes=app_backoff)
        apply_changes = options["apply"]

        for credential in credentials:
            real_expiry_by_platform: dict[str, object] = {}
            logs = (
                PostLog.objects.filter(
                    target__credential=credential,
                    status=PostLog.STATUS_FAILED,
                    created_at__gte=window_start,
                )
                .exclude(message="")
                .order_by("-created_at")
            )
            for log in logs:
                message = (log.message or "").lower()
                if message.startswith(SYNTHETIC_PREFIXES):
                    continue
                if not any(marker in message for marker in RATE_LIMIT_MARKERS):
                    continue
                expiry = log.created_at + timedelta(minutes=app_backoff)
                current = real_expiry_by_platform.get(log.platform)
                if current is None or expiry > current:
                    real_expiry_by_platform[log.platform] = expiry

            self.stdout.write(f"CREDENTIAL id={credential.id} label={credential.label!r} real_expiry={real_expiry_by_platform}")
            runs = ScheduledPostRun.objects.filter(target__credential=credential, status=ScheduledPostRun.STATUS_BACKOFF).select_related("target")
            for run in runs.order_by("target_id", "scheduled_for"):
                total += 1
                platform_status = dict(run.platform_status or {})
                candidate_expiries = [
                    real_expiry_by_platform[platform]
                    for platform, status in platform_status.items()
                    if status == ScheduledPostRun.STATUS_BACKOFF and platform in real_expiry_by_platform
                ]
                if not candidate_expiries:
                    continue
                desired_retry_at = max(candidate_expiries)
                if run.next_retry_at and run.next_retry_at <= desired_retry_at:
                    continue
                self.stdout.write(
                    "CLAMP "
                    f"run={run.id} target={run.target_id} slot={timezone.localtime(run.scheduled_for)} "
                    f"old={timezone.localtime(run.next_retry_at) if run.next_retry_at else None} "
                    f"new={timezone.localtime(desired_retry_at)} file={run.drive_file_name!r}"
                )
                fixed += 1
                if apply_changes:
                    run.next_retry_at = desired_retry_at
                    run.save(update_fields=["next_retry_at", "updated_at"])

        if apply_changes:
            cache.clear()
        self.stdout.write(f"checked_backoff_runs={total} fixed={fixed} applied={apply_changes}")
