from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

from scheduler.models import MetaCredential, MetaCredentialEvent, PublishingTarget, SocialAccount


class Command(BaseCommand):
    help = "Print Meta credential/account/target inventory and the active database path."

    def handle(self, *args, **options):
        db_name = connection.settings_dict.get("NAME")
        self.stdout.write(f"database_engine={connection.settings_dict.get('ENGINE')}")
        self.stdout.write(f"database_name={db_name}")
        self.stdout.write(f"base_dir={settings.BASE_DIR}")
        self.stdout.write(
            "counts: "
            f"credentials={MetaCredential.objects.count()} "
            f"accounts={SocialAccount.objects.count()} "
            f"targets={PublishingTarget.objects.count()} "
            f"active_targets={PublishingTarget.objects.filter(is_active=True).count()}"
        )
        for credential in MetaCredential.objects.all().order_by("id"):
            self.stdout.write(
                "CREDENTIAL "
                f"id={credential.id} "
                f"label={credential.label!r} "
                f"active={credential.is_active} "
                f"user={credential.user_name!r} "
                f"user_id={credential.user_id!r} "
                f"accounts={credential.accounts.count()} "
                f"targets={credential.targets.count()} "
                f"created={credential.created_at.isoformat()} "
                f"updated={credential.updated_at.isoformat()} "
                f"last_sync={credential.last_sync_at.isoformat() if credential.last_sync_at else '-'} "
                f"error={(credential.last_error or '')[:180]!r}"
            )
            latest_event = credential.events.order_by("-created_at", "-id").first()
            if latest_event:
                self.stdout.write(
                    "  LATEST_EVENT "
                    f"id={latest_event.id} "
                    f"action={latest_event.action} "
                    f"source={latest_event.source!r} "
                    f"actor={latest_event.actor!r} "
                    f"created={latest_event.created_at.isoformat()} "
                    f"note={latest_event.note[:180]!r}"
                )
        for target in PublishingTarget.objects.select_related("credential", "facebook_account", "instagram_account").order_by("id"):
            self.stdout.write(
                "TARGET "
                f"id={target.id} "
                f"name={target.display_name!r} "
                f"cred={target.credential_id}:{target.credential.label!r} "
                f"active={target.is_active} "
                f"fb={target.facebook_account_id} "
                f"ig={target.instagram_account_id} "
                f"drive={target.drive_folder_id!r} "
                f"status={target.last_status!r} "
                f"error={(target.last_error or '')[:180]!r}"
            )
        self.stdout.write(f"events={MetaCredentialEvent.objects.count()}")
