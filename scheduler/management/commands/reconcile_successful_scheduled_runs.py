from django.core.management.base import BaseCommand

from scheduler.models import PublishingTarget, ScheduledPostRun
from scheduler.services.publishing import _active_platforms, _sync_run_successes


class Command(BaseCommand):
    help = "Mark non-terminal scheduled runs complete when their assigned file already succeeded on all active platforms."

    def add_arguments(self, parser):
        parser.add_argument("--target-id", type=int, action="append")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        targets = PublishingTarget.objects.filter(is_active=True).select_related(
            "credential",
            "facebook_account",
            "instagram_account",
        )
        if options["target_id"]:
            targets = targets.filter(id__in=options["target_id"])

        checked = 0
        reconciled = 0
        apply_changes = options["apply"]

        runs = ScheduledPostRun.objects.filter(
            target__in=targets,
        ).exclude(
            status__in=ScheduledPostRun.TERMINAL_STATUSES,
        ).select_related(
            "target",
            "target__facebook_account",
            "target__instagram_account",
        ).order_by("target_id", "scheduled_for")

        for run in runs:
            checked += 1
            active_platforms = set(_active_platforms(run.target))
            old_status = run.status
            old_platform_status = dict(run.platform_status or {})
            _sync_run_successes(run, active_platforms)
            if run.status != ScheduledPostRun.STATUS_SUCCESS:
                continue
            reconciled += 1
            self.stdout.write(
                "RECONCILE "
                f"run={run.id} target={run.target_id} file={run.drive_file_name!r} "
                f"old_status={old_status} old_platforms={old_platform_status}"
            )
            if apply_changes:
                run.save(update_fields=["drive_file_id", "drive_file_name", "status", "platform_status", "next_retry_at", "last_error", "updated_at"])

        self.stdout.write(f"checked={checked} reconciled={reconciled} applied={apply_changes}")
