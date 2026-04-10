from django.core.management.base import BaseCommand

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
            for issue in health["issues"]:
                self._safe_write(f"  - {issue}")
