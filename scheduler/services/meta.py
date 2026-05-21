from __future__ import annotations

from dataclasses import dataclass

import requests
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from scheduler.models import MetaCredential, PublishingTarget, SocialAccount


class MetaAPIError(Exception):
    pass


@dataclass
class AssetBundle:
    pages: list[dict]
    instagram_accounts: list[dict]
    me: dict


def _graph_get(path: str, access_token: str, params: dict | None = None) -> dict:
    query = {"access_token": access_token}
    if params:
        query.update(params)
    response = requests.get(f"{settings.META_GRAPH_BASE_URL}{path}", params=query, timeout=30)
    try:
        data = response.json()
    except ValueError:
        snippet = (response.text or "").strip()[:300] or "<empty response body>"
        raise MetaAPIError(f"Meta returned a non-JSON response (status {response.status_code}): {snippet}")
    if response.status_code >= 400 or data.get("error"):
        message = data.get("error", {}).get("message", response.text)
        raise MetaAPIError(message)
    return data


def _fetch_pages(access_token: str) -> list[dict]:
    page_fields = "id,name,access_token,instagram_business_account{id,username,name},connected_instagram_account{id,username,name}"
    errors = []

    try:
        result = _graph_get("/me/accounts", access_token, {"fields": page_fields})
        return result.get("data", [])
    except MetaAPIError as exc:
        errors.append(str(exc))

    try:
        result = _graph_get("/me", access_token, {"fields": f"accounts{{{page_fields}}}"})
        return result.get("accounts", {}).get("data", [])
    except MetaAPIError as exc:
        errors.append(str(exc))

    raise MetaAPIError(
        "Unable to fetch Facebook pages. Use a long-lived user token with pages_show_list and business_management permissions. "
        f"Meta response: {' | '.join(errors)}"
    )


def fetch_meta_assets(access_token: str) -> AssetBundle:
    me = _graph_get("/me", access_token, {"fields": "id,name"})
    page_items = _fetch_pages(access_token)
    instagram_map = {}
    for page in page_items:
        for key in ("instagram_business_account", "connected_instagram_account"):
            ig = page.get(key)
            if ig and ig.get("id"):
                instagram_map[ig["id"]] = ig

    for field_name in (
        "instagram_accounts{id,username,name}",
        "businesses{instagram_accounts{id,username,name},owned_instagram_accounts{id,username,name}}",
    ):
        try:
            extra = _graph_get("/me", access_token, {"fields": field_name})
        except MetaAPIError:
            continue
        for item in extra.get("instagram_accounts", []):
            instagram_map[item["id"]] = item
        for business in extra.get("businesses", {}).get("data", []):
            for bucket in ("instagram_accounts", "owned_instagram_accounts"):
                for item in business.get(bucket, {}).get("data", []):
                    instagram_map[item["id"]] = item

    return AssetBundle(pages=page_items, instagram_accounts=list(instagram_map.values()), me=me)


def _build_sync_key(facebook_id: str = "", instagram_id: str = "") -> str:
    if facebook_id and instagram_id:
        return f"fb:{facebook_id}|ig:{instagram_id}"
    if facebook_id:
        return f"fb:{facebook_id}"
    return f"ig:{instagram_id}"


def _target_has_scheduling_state(target: PublishingTarget) -> bool:
    return bool(
        target.drive_folder_id
        or target.drive_folder_url
        or target.default_caption.strip()
        or target.posting_times
        or target.posts_per_day != 1
        or target.ai_enabled
        or target.ai_auto_caption_enabled
        or target.post_logs.exists()
        or target.media_assets.exists()
    )


def _target_match_strength(target: PublishingTarget, facebook_id: str = "", instagram_id: str = "") -> int:
    fb_matches = bool(facebook_id and target.facebook_account and target.facebook_account.external_id == facebook_id)
    ig_matches = bool(instagram_id and target.instagram_account and target.instagram_account.external_id == instagram_id)
    if facebook_id and instagram_id and fb_matches and ig_matches:
        return 3
    if facebook_id and fb_matches:
        return 2
    if instagram_id and ig_matches:
        return 2
    return 1


def _target_lookup_conditions(facebook_id: str = "", instagram_id: str = "") -> Q:
    conditions = Q()
    sync_keys = []
    if facebook_id:
        conditions |= Q(facebook_account__external_id=facebook_id)
        sync_keys.append(_build_sync_key(facebook_id=facebook_id))
    if instagram_id:
        conditions |= Q(instagram_account__external_id=instagram_id)
        sync_keys.append(_build_sync_key(instagram_id=instagram_id))
    if facebook_id and instagram_id:
        sync_keys.append(_build_sync_key(facebook_id=facebook_id, instagram_id=instagram_id))
    if sync_keys:
        conditions |= Q(sync_key__in=sync_keys)
    return conditions


def _find_existing_target(facebook_id: str = "", instagram_id: str = "") -> PublishingTarget | None:
    conditions = _target_lookup_conditions(facebook_id=facebook_id, instagram_id=instagram_id)
    if not conditions:
        return None

    candidates = list(
        PublishingTarget.objects.filter(conditions)
        .select_related("facebook_account", "instagram_account")
        .distinct()
    )
    if not candidates:
        return None

    candidates.sort(
        key=lambda target: (
            _target_has_scheduling_state(target),
            _target_match_strength(target, facebook_id, instagram_id),
            target.is_active,
            target.updated_at,
            -target.pk,
        ),
        reverse=True,
    )
    return candidates[0]


def _retire_duplicate_targets(target: PublishingTarget, facebook_id: str = "", instagram_id: str = "") -> None:
    conditions = _target_lookup_conditions(facebook_id=facebook_id, instagram_id=instagram_id)
    if not conditions:
        return
    duplicates = PublishingTarget.objects.filter(conditions).exclude(pk=target.pk).distinct()
    for duplicate in duplicates:
        duplicate.is_active = False
        duplicate.last_error = f"Merged into target {target.pk} during Meta sync. Existing settings were preserved on this row."
        update_fields = ["is_active", "last_error", "updated_at"]
        if duplicate.sync_key == _build_sync_key(facebook_id=facebook_id, instagram_id=instagram_id):
            duplicate.sync_key = f"archived:{duplicate.pk}:{duplicate.sync_key}"[:255]
            update_fields.append("sync_key")
        duplicate.save(update_fields=update_fields)


def _apply_target_links(
    target: PublishingTarget,
    *,
    credential: MetaCredential,
    display_name: str,
    facebook_account: SocialAccount | None,
    instagram_account: SocialAccount | None,
    sync_key: str,
) -> None:
    facebook_id = facebook_account.external_id if facebook_account else ""
    instagram_id = instagram_account.external_id if instagram_account else ""
    _retire_duplicate_targets(target, facebook_id=facebook_id, instagram_id=instagram_id)

    target.credential = credential
    target.display_name = display_name
    target.facebook_account = facebook_account
    target.instagram_account = instagram_account
    target.last_error = ""

    update_fields = [
        "credential",
        "display_name",
        "facebook_account",
        "instagram_account",
        "last_error",
        "updated_at",
    ]
    sync_key_conflict = PublishingTarget.objects.filter(sync_key=sync_key).exclude(pk=target.pk).exists()
    if not sync_key_conflict and target.sync_key != sync_key:
        target.sync_key = sync_key
        update_fields.append("sync_key")
    target.save(update_fields=update_fields)


def _get_or_create_target(
    *,
    credential: MetaCredential,
    display_name: str,
    facebook_account: SocialAccount | None = None,
    instagram_account: SocialAccount | None = None,
) -> tuple[PublishingTarget, bool]:
    facebook_id = facebook_account.external_id if facebook_account else ""
    instagram_id = instagram_account.external_id if instagram_account else ""
    sync_key = _build_sync_key(facebook_id=facebook_id, instagram_id=instagram_id)
    target = _find_existing_target(facebook_id=facebook_id, instagram_id=instagram_id)
    if target:
        _apply_target_links(
            target,
            credential=credential,
            display_name=display_name,
            facebook_account=facebook_account,
            instagram_account=instagram_account,
            sync_key=sync_key,
        )
        return target, False

    return (
        PublishingTarget.objects.create(
            credential=credential,
            sync_key=sync_key,
            display_name=display_name,
            facebook_account=facebook_account,
            instagram_account=instagram_account,
        ),
        True,
    )


def sync_credential_accounts(credential: MetaCredential) -> dict[str, int]:
    try:
        assets = fetch_meta_assets(credential.access_token)
    except MetaAPIError as exc:
        credential.last_error = str(exc)
        credential.last_sync_at = timezone.now()
        credential.save(update_fields=["last_error", "last_sync_at", "updated_at"])
        raise

    credential.user_id = assets.me.get("id", credential.user_id)
    credential.user_name = assets.me.get("name", credential.user_name)
    credential.token_last_validated_at = timezone.now()
    credential.last_sync_at = timezone.now()
    credential.last_error = ""
    credential.save(update_fields=["user_id", "user_name", "token_last_validated_at", "last_sync_at", "last_error", "updated_at"])

    seen_target_ids = set()
    linked_ig_ids = set()
    created_count = 0
    reused_count = 0

    for page in assets.pages:
        fb_account, _ = SocialAccount.objects.update_or_create(
            credential=credential,
            platform=SocialAccount.FACEBOOK,
            external_id=page["id"],
            defaults={
                "name": page.get("name", ""),
                "access_token": page.get("access_token", ""),
                "raw_payload": page,
                "is_active": True,
            },
        )

        linked_ig = page.get("instagram_business_account") or page.get("connected_instagram_account")
        ig_account = None
        if linked_ig:
            linked_ig_ids.add(linked_ig["id"])
            ig_account, _ = SocialAccount.objects.update_or_create(
                credential=credential,
                platform=SocialAccount.INSTAGRAM,
                external_id=linked_ig["id"],
                defaults={
                    "name": linked_ig.get("name", ""),
                    "username": linked_ig.get("username", ""),
                    "access_token": page.get("access_token", ""),
                    "raw_payload": linked_ig,
                    "is_active": True,
                },
            )

        display_name = f"{fb_account.display_name} + {ig_account.display_name}" if ig_account else fb_account.display_name
        target, created = _get_or_create_target(
            credential=credential,
            display_name=display_name,
            facebook_account=fb_account,
            instagram_account=ig_account,
        )
        created_count += 1 if created else 0
        reused_count += 0 if created else 1
        seen_target_ids.add(target.pk)

    for ig in assets.instagram_accounts:
        ig_account, _ = SocialAccount.objects.update_or_create(
            credential=credential,
            platform=SocialAccount.INSTAGRAM,
            external_id=ig["id"],
            defaults={
                "name": ig.get("name", ""),
                "username": ig.get("username", ""),
                "raw_payload": ig,
                "is_active": True,
            },
        )
        if ig_account.external_id in linked_ig_ids:
            continue
        target, created = _get_or_create_target(
            credential=credential,
            display_name=ig_account.display_name,
            instagram_account=ig_account,
        )
        created_count += 1 if created else 0
        reused_count += 0 if created else 1
        seen_target_ids.add(target.pk)

    missing_queryset = credential.targets.exclude(pk__in=seen_target_ids).filter(is_active=True)
    missing_count = missing_queryset.count()
    missing_queryset.update(
        last_error="Target not returned in latest Meta sync. Existing scheduling settings were preserved.",
        updated_at=timezone.now(),
    )
    return {"created": created_count, "reused": reused_count, "missing": missing_count}
