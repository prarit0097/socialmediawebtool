from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from scheduler.models import PostLog, PublishingTarget, ScheduledPostRun, SocialAccount
from scheduler.services.publishing import _active_platforms, _sync_run_successes


class Command(BaseCommand):
    help = "Mark one target/file/platform as skipped so the queue can move past a stuck media item."

    def add_arguments(self, parser):
        parser.add_argument("--target-id", type=int, required=True)
        parser.add_argument("--drive-file-id", required=True)
        parser.add_argument("--platform", choices=[SocialAccount.FACEBOOK, SocialAccount.INSTAGRAM], required=True)
        parser.add_argument("--reason", default="Manually skipped stuck media/platform after repeated publish blockage.")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        target = PublishingTarget.objects.select_related("facebook_account", "instagram_account").get(pk=options["target_id"])
        drive_file_id = options["drive_file_id"]
        platform = options["platform"]
        if platform not in _active_platforms(target):
            raise CommandError(f"{platform} is not active for target {target.id}.")

        existing_success = target.post_logs.filter(
            platform=platform,
            drive_file_id=drive_file_id,
            status=PostLog.STATUS_SUCCESS,
        ).exists()
        if existing_success:
            self.stdout.write(f"SKIP_NOT_NEEDED target={target.id} platform={platform} file={drive_file_id}: already success")
            return

        runs = list(
            target.scheduled_runs.filter(drive_file_id=drive_file_id)
            .exclude(status__in=ScheduledPostRun.TERMINAL_STATUSES)
            .order_by("scheduled_for")
        )
        if not runs:
            runs = list(target.scheduled_runs.filter(drive_file_id=drive_file_id).order_by("scheduled_for")[:1])

        self.stdout.write(
            f"SKIP target={target.id} platform={platform} file={drive_file_id} "
            f"runs={len(runs)} applied={options['apply']}"
        )
        if not options["apply"]:
            for run in runs:
                self.stdout.write(f"  WOULD_UPDATE run={run.id} status={run.status} scheduled_for={timezone.localtime(run.scheduled_for)}")
            return

        scheduled_for = runs[-1].scheduled_for if runs else timezone.now()
        drive_file_name = runs[-1].drive_file_name if runs else ""
        PostLog.objects.get_or_create(
            target=target,
            platform=platform,
            drive_file_id=drive_file_id,
            status=PostLog.STATUS_SKIPPED,
            defaults={
                "scheduled_for": scheduled_for,
                "drive_file_name": drive_file_name,
                "message": options["reason"],
            },
        )
        active_platforms = set(_active_platforms(target))
        for run in runs:
            _sync_run_successes(run, active_platforms)
            run.lock_owner = ""
            run.locked_at = None
            run.save(
                update_fields=[
                    "drive_file_id",
                    "drive_file_name",
                    "status",
                    "platform_status",
                    "next_retry_at",
                    "last_error",
                    "lock_owner",
                    "locked_at",
                    "updated_at",
                ]
            )
            self.stdout.write(f"  UPDATED run={run.id} status={run.status} platforms={run.platform_status}")

        target.last_status = "skipped"
        target.last_error = options["reason"]
        target.save(update_fields=["last_status", "last_error", "updated_at"])
