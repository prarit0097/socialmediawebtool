from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from scheduler.models import PostLog, PublishingTarget, ScheduledPostRun, SocialAccount
from scheduler.services.publishing import _active_platforms, _sync_run_successes


class Command(BaseCommand):
    help = "Mark a manually verified live platform/file as success without publishing again."

    def add_arguments(self, parser):
        parser.add_argument("--target-id", type=int, required=True)
        parser.add_argument("--drive-file-id", required=True)
        parser.add_argument("--platform", choices=[SocialAccount.FACEBOOK, SocialAccount.INSTAGRAM], required=True)
        parser.add_argument("--meta-id", default="manual-live-reconciled")
        parser.add_argument("--message", default="Manually reconciled: post was verified live on platform after Meta/app uncertainty.")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        target = PublishingTarget.objects.select_related("facebook_account", "instagram_account").get(pk=options["target_id"])
        drive_file_id = options["drive_file_id"]
        platform = options["platform"]
        if platform not in _active_platforms(target):
            raise CommandError(f"{platform} is not active for target {target.id}.")

        runs = list(target.scheduled_runs.filter(drive_file_id=drive_file_id).order_by("scheduled_for"))
        drive_file_name = ""
        scheduled_for = timezone.now()
        if runs:
            drive_file_name = runs[-1].drive_file_name
            scheduled_for = runs[-1].scheduled_for

        existing = target.post_logs.filter(platform=platform, drive_file_id=drive_file_id, status=PostLog.STATUS_SUCCESS).first()
        self.stdout.write(
            f"RECONCILE_LIVE target={target.id} platform={platform} file={drive_file_id} "
            f"runs={len(runs)} existing_success={bool(existing)} applied={options['apply']}"
        )
        if not options["apply"]:
            for run in runs:
                self.stdout.write(f"  WOULD_UPDATE run={run.id} status={run.status} scheduled_for={timezone.localtime(run.scheduled_for)}")
            return

        if not existing:
            PostLog.objects.create(
                target=target,
                platform=platform,
                scheduled_for=scheduled_for,
                published_at=timezone.now(),
                status=PostLog.STATUS_SUCCESS,
                drive_file_id=drive_file_id,
                drive_file_name=drive_file_name,
                meta_creation_id=options["meta_id"],
                message=options["message"],
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

        target.last_status = "success"
        target.last_error = ""
        target.last_posted_at = timezone.now()
        target.save(update_fields=["last_status", "last_error", "last_posted_at", "updated_at"])
