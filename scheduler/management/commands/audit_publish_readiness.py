from django.core.management.base import BaseCommand
from django.utils import timezone

from scheduler.models import PublishingTarget
from scheduler.services.health import build_target_health


class Command(BaseCommand):
    help = "Audit all active targets for production publish readiness without changing DB state."

    def _safe_write(self, message: str) -> None:
        self.stdout.write(message.encode("ascii", "backslashreplace").decode("ascii"))

    def handle(self, *args, **options):
        targets = PublishingTarget.objects.filter(is_active=True).select_related("facebook_account", "instagram_account", "credential")
        if not targets.exists():
            self._safe_write("No active targets configured.")
            return

        for target in targets:
            health = build_target_health(target)
            self._safe_write(f"[{target.pk}] {target.display_name} :: {health['overall']}")
            self._safe_write(f"  token: {target.credential.label}")
            self._safe_write(f"  platforms: {', '.join(health['active_platforms']) or 'none'}")
            self._safe_write(f"  public_url_ready: {'yes' if health['public_base_ready'] else 'no'}")
            self._safe_write(f"  drive: {target.drive_folder_id or 'not set'} | files={health['file_count']} media={health['media_count']} cached={health['cached_asset_count']}")
            if health["current_file"]:
                self._safe_write(
                    "  current_file: "
                    f"{health['current_file'].get('name', 'unknown')} "
                    f"({health['current_file'].get('mimeType', 'unknown')})"
                )
            if health["pending_platforms"]:
                self._safe_write(f"  pending_platforms: {', '.join(health['pending_platforms'])}")
            if health["content_exhausted"]:
                self._safe_write("  content: exhausted; add new Drive media files.")
            if health["next_upcoming_slot"]:
                self._safe_write(f"  next_upcoming_slot: {timezone.localtime(health['next_upcoming_slot']).strftime('%Y-%m-%d %H:%M')}")
            if health["due_slots"]:
                slots = ", ".join(
                    f"{timezone.localtime(item['slot']).strftime('%H:%M')}={item['status']}"
                    for item in health["due_slots"]
                )
                self._safe_write(f"  slots_today: {slots}")
            if health["latest_success"]:
                latest = health["latest_success"]
                self._safe_write(
                    "  latest_success: "
                    f"{latest['platform']} {latest['drive_file_name']} "
                    f"at {timezone.localtime(latest['published_at']).strftime('%Y-%m-%d %H:%M')}"
                )
            if health["latest_failure"]:
                latest = health["latest_failure"]
                reason = (latest["message"] or "").replace("\n", " ")[:220]
                self._safe_write(
                    "  latest_failure: "
                    f"{latest['platform']} {latest['drive_file_name']} "
                    f"at {timezone.localtime(latest['created_at']).strftime('%Y-%m-%d %H:%M')} :: {reason}"
                )
            for message in health["backoff_messages"]:
                self._safe_write(f"  backoff: {message}")
            for issue in health["issues"]:
                self._safe_write(f"  - {issue}")
