from django.core.management.base import BaseCommand

from scheduler.models import MetaCredential
from scheduler.services.meta import MetaAPIError, sync_credential_accounts


class Command(BaseCommand):
    help = "Sync Meta pages and Instagram accounts for all active credentials."

    def handle(self, *args, **options):
        for credential in MetaCredential.objects.filter(is_active=True):
            try:
                sync_credential_accounts(credential)
                self.stdout.write(self.style.SUCCESS(f"Synced {credential.label}"))
            except MetaAPIError as exc:
                self.stderr.write(f"Failed {credential.label}: {exc}")
