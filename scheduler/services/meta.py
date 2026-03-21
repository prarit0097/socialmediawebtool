from __future__ import annotations

from dataclasses import dataclass

import requests
from django.conf import settings
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
    data = response.json()
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


def sync_credential_accounts(credential: MetaCredential) -> None:
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

    seen_keys = set()
    linked_ig_ids = set()

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

        sync_key = f"fb:{fb_account.external_id}|ig:{ig_account.external_id}" if ig_account else f"fb:{fb_account.external_id}"
        display_name = f"{fb_account.display_name} + {ig_account.display_name}" if ig_account else fb_account.display_name
        target, created = PublishingTarget.objects.get_or_create(
            sync_key=sync_key,
            defaults={
                "credential": credential,
                "display_name": display_name,
                "facebook_account": fb_account,
                "instagram_account": ig_account,
            },
        )
        if not created:
            target.credential = credential
            target.display_name = display_name
            target.facebook_account = fb_account
            target.instagram_account = ig_account
            target.is_active = True
            target.save(update_fields=["credential", "display_name", "facebook_account", "instagram_account", "is_active", "updated_at"])
        seen_keys.add(sync_key)

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
        sync_key = f"ig:{ig_account.external_id}"
        target, created = PublishingTarget.objects.get_or_create(
            sync_key=sync_key,
            defaults={"credential": credential, "display_name": ig_account.display_name, "instagram_account": ig_account},
        )
        if not created:
            target.credential = credential
            target.display_name = ig_account.display_name
            target.instagram_account = ig_account
            target.facebook_account = None
            target.is_active = True
            target.save(update_fields=["credential", "display_name", "instagram_account", "facebook_account", "is_active", "updated_at"])
        seen_keys.add(sync_key)

    credential.targets.exclude(sync_key__in=seen_keys).update(is_active=False, last_error="Target not returned in latest Meta sync.")
