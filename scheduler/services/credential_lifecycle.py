from __future__ import annotations

from django.utils import timezone

from scheduler.models import MetaCredential, MetaCredentialEvent


def _actor_from_request(request) -> str:
    if request is None:
        return ""
    user = getattr(request, "user", None)
    if user and getattr(user, "is_authenticated", False):
        return str(user)
    meta = getattr(request, "META", {})
    return meta.get("REMOTE_ADDR", "")


def build_credential_snapshot(credential: MetaCredential) -> dict:
    accounts = []
    for account in credential.accounts.order_by("id"):
        accounts.append(
            {
                "id": account.id,
                "platform": account.platform,
                "external_id": account.external_id,
                "name": account.name,
                "username": account.username,
                "is_active": account.is_active,
            }
        )
    targets = []
    for target in credential.targets.order_by("id"):
        targets.append(
            {
                "id": target.id,
                "sync_key": target.sync_key,
                "display_name": target.display_name,
                "facebook_account_id": target.facebook_account_id,
                "instagram_account_id": target.instagram_account_id,
                "drive_folder_id": target.drive_folder_id,
                "drive_folder_url": target.drive_folder_url,
                "posts_per_day": target.posts_per_day,
                "posting_times": target.posting_times,
                "is_active": target.is_active,
                "last_status": target.last_status,
                "last_error": target.last_error,
            }
        )
    return {
        "credential": {
            "id": credential.id,
            "label": credential.label,
            "user_name": credential.user_name,
            "user_id": credential.user_id,
            "is_active": credential.is_active,
            "last_sync_at": credential.last_sync_at.isoformat() if credential.last_sync_at else "",
            "token_last_validated_at": credential.token_last_validated_at.isoformat() if credential.token_last_validated_at else "",
            "last_error": credential.last_error,
            "created_at": credential.created_at.isoformat() if credential.created_at else "",
            "updated_at": credential.updated_at.isoformat() if credential.updated_at else "",
        },
        "accounts": accounts,
        "targets": targets,
    }


def record_credential_event(
    credential: MetaCredential,
    *,
    action: str,
    source: str = "",
    actor: str = "",
    note: str = "",
    snapshot: dict | None = None,
) -> MetaCredentialEvent:
    return MetaCredentialEvent.objects.create(
        credential=credential,
        credential_label=credential.label,
        action=action,
        source=source,
        actor=actor,
        note=note,
        snapshot=snapshot if snapshot is not None else build_credential_snapshot(credential),
    )


def archive_credential(credential: MetaCredential, *, source: str = "", actor: str = "", note: str = "") -> MetaCredentialEvent:
    event = record_credential_event(
        credential,
        action=MetaCredentialEvent.ACTION_ARCHIVED,
        source=source,
        actor=actor,
        note=note or "Archived credential before disabling linked targets.",
    )
    credential.is_active = False
    credential.last_error = "Archived. Existing accounts and targets were preserved."
    credential.save(update_fields=["is_active", "last_error", "updated_at"])
    credential.targets.filter(is_active=True).update(
        is_active=False,
        last_status="failed",
        last_error="Paused because the Meta credential was archived.",
        updated_at=timezone.now(),
    )
    return event


def restore_credential(credential: MetaCredential, *, source: str = "", actor: str = "", note: str = "") -> MetaCredentialEvent:
    latest_archive = credential.events.filter(action=MetaCredentialEvent.ACTION_ARCHIVED).order_by("-created_at", "-id").first()
    active_target_ids = set()
    if latest_archive:
        for target_data in latest_archive.snapshot.get("targets", []):
            if target_data.get("is_active"):
                active_target_ids.add(target_data.get("id"))

    credential.is_active = True
    credential.last_error = ""
    credential.save(update_fields=["is_active", "last_error", "updated_at"])
    if active_target_ids:
        credential.targets.filter(id__in=active_target_ids).update(is_active=True, last_error="", updated_at=timezone.now())

    return record_credential_event(
        credential,
        action=MetaCredentialEvent.ACTION_RESTORED,
        source=source,
        actor=actor,
        note=note or "Restored credential from latest archive snapshot.",
    )


def archive_credential_from_request(credential: MetaCredential, request, *, source: str = "dashboard") -> MetaCredentialEvent:
    return archive_credential(credential, source=source, actor=_actor_from_request(request))


def restore_credential_from_request(credential: MetaCredential, request, *, source: str = "dashboard") -> MetaCredentialEvent:
    return restore_credential(credential, source=source, actor=_actor_from_request(request))
