from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import time

import requests
from django.conf import settings
from django.utils import timezone

from scheduler.models import PostLog, PublishingTarget, SocialAccount
from scheduler.services.cache import get_cached_public_urls
from scheduler.services.diagnostics import build_rejection_diagnostics
from scheduler.services.drive import (
    DriveConfigError,
    ensure_public_file,
    find_caption_file,
    get_drive_service,
    get_public_media_urls,
    get_publishable_file_url,
    is_publishable_media,
    list_folder_files,
)
from scheduler.services.proxy import build_proxy_urls, is_public_base_ready


class PublishingError(Exception):
    pass


def _graph_post(path: str, access_token: str, payload: dict) -> dict:
    response = requests.post(f"{settings.META_GRAPH_BASE_URL}{path}", data={**payload, "access_token": access_token}, timeout=60)
    data = response.json()
    if response.status_code >= 400 or data.get("error"):
        message = data.get("error", {}).get("message", response.text)
        raise PublishingError(message)
    return data


def _graph_get(path: str, access_token: str, params: dict | None = None) -> dict:
    query = {"access_token": access_token}
    if params:
        query.update(params)
    response = requests.get(f"{settings.META_GRAPH_BASE_URL}{path}", params=query, timeout=60)
    data = response.json()
    if response.status_code >= 400 or data.get("error"):
        message = data.get("error", {}).get("message", response.text)
        raise PublishingError(message)
    return data


def get_daily_slots(target: PublishingTarget, day=None) -> list[datetime]:
    now = timezone.localtime(day or timezone.now())
    explicit_times = target.posting_times or []
    if len(explicit_times) == target.posts_per_day:
        slots = []
        for value in explicit_times:
            slot_time = datetime.strptime(value, "%H:%M").time()
            slots.append(timezone.make_aware(datetime.combine(now.date(), slot_time)))
        return sorted(slots)
    start_dt = timezone.make_aware(datetime.combine(now.date(), target.posting_window_start))
    end_dt = timezone.make_aware(datetime.combine(now.date(), target.posting_window_end))
    if target.posts_per_day == 1:
        return [start_dt]
    interval = (end_dt - start_dt) / (target.posts_per_day - 1)
    return [start_dt + interval * index for index in range(target.posts_per_day)]


def pick_next_file(target: PublishingTarget) -> dict:
    files = list_folder_files(target.drive_folder_id)
    media_files = [file_obj for file_obj in files if is_publishable_media(file_obj)]
    used_ids = list(
        target.post_logs.filter(status=PostLog.STATUS_SUCCESS)
        .exclude(drive_file_id="")
        .values_list("drive_file_id", flat=True)
    )
    for file_obj in media_files:
        if file_obj["id"] not in used_ids:
            return file_obj
    if media_files:
        raise PublishingError("All unique media files in the configured Google Drive folder have already been published. Add new files to continue posting.")
    raise PublishingError("No publishable image or video files found in the configured Google Drive folder.")


def _active_platforms(target: PublishingTarget) -> list[str]:
    platforms = []
    if target.facebook_account:
        platforms.append(SocialAccount.FACEBOOK)
    if target.instagram_account:
        platforms.append(SocialAccount.INSTAGRAM)
    return platforms


def _get_slot_locked_file(target: PublishingTarget, scheduled_for) -> dict:
    locked_file_id = (
        target.post_logs.filter(scheduled_for=scheduled_for)
        .exclude(drive_file_id="")
        .order_by("created_at")
        .values_list("drive_file_id", flat=True)
        .first()
    )
    if not locked_file_id:
        return pick_next_shared_file(target)

    files = list_folder_files(target.drive_folder_id)
    for file_obj in files:
        if file_obj.get("id") == locked_file_id and is_publishable_media(file_obj):
            return file_obj
    raise PublishingError("The media file already assigned to this slot is no longer available in Drive.")


def pick_next_shared_file(target: PublishingTarget) -> dict:
    files = list_folder_files(target.drive_folder_id)
    media_files = [file_obj for file_obj in files if is_publishable_media(file_obj)]
    active_platforms = set(_active_platforms(target))
    success_rows = list(
        target.post_logs.filter(status=PostLog.STATUS_SUCCESS)
        .exclude(drive_file_id="")
        .values("drive_file_id", "platform")
    )
    success_map = {}
    for row in success_rows:
        success_map.setdefault(row["drive_file_id"], set()).add(row["platform"])

    for file_obj in media_files:
        if success_map.get(file_obj["id"], set()) != active_platforms:
            return file_obj
    if media_files:
        raise PublishingError("All unique media files in the configured Google Drive folder have already been published on every active platform. Add new files to continue posting.")
    raise PublishingError("No publishable image or video files found in the configured Google Drive folder.")


def _platform_already_succeeded_for_file(target: PublishingTarget, platform: str, drive_file_id: str) -> bool:
    return target.post_logs.filter(
        platform=platform,
        drive_file_id=drive_file_id,
        status=PostLog.STATUS_SUCCESS,
    ).exists()


def _slot_is_complete(target: PublishingTarget, scheduled_for, active_platforms: set[str]) -> bool:
    if not active_platforms:
        return False
    slot_successes = set(
        target.post_logs.filter(
            scheduled_for=scheduled_for,
            status=PostLog.STATUS_SUCCESS,
        ).values_list("platform", flat=True)
    )
    return slot_successes == active_platforms


def build_caption(target: PublishingTarget) -> str:
    if target.default_caption.strip():
        return target.default_caption.strip().replace("\r\n", "\n").replace("\r", "\n")

    files = list_folder_files(target.drive_folder_id)
    caption_file = find_caption_file(files)
    if not caption_file:
        return ""

    response = requests.get(get_publishable_file_url(caption_file), timeout=30)
    response.raise_for_status()
    return response.text.strip().replace("\r\n", "\n").replace("\r", "\n")


def _build_media_title(file_obj: dict) -> str:
    name = file_obj.get("name", "Media")
    stem = Path(name).stem.strip() or "Media"
    return stem[:120]


def _publish_to_facebook(target: PublishingTarget, file_obj: dict) -> str:
    if not target.facebook_account:
        return ""
    token = target.facebook_account.access_token or target.credential.access_token
    if not token:
        raise PublishingError("Facebook page access token not available.")
    caption = build_caption(target) or file_obj["name"]
    media_urls = get_cached_public_urls(target, file_obj, variant="default")
    if not media_urls:
        media_urls = build_proxy_urls(target.id, file_obj["id"], file_obj.get("name", "media")) if is_public_base_ready() else []
    if not media_urls:
        media_urls = get_public_media_urls(file_obj)
    mime_type = file_obj.get("mimeType", "")
    errors = []
    for media_url in media_urls:
        try:
            if mime_type.startswith("video/"):
                result = _graph_post(
                    f"/{target.facebook_account.external_id}/videos",
                    token,
                    {
                        "file_url": media_url,
                        "title": _build_media_title(file_obj),
                        "description": caption,
                        "published": "true",
                    },
                )
            else:
                result = _graph_post(
                    f"/{target.facebook_account.external_id}/photos",
                    token,
                    {
                        "url": media_url,
                        "caption": caption,
                        "published": "true",
                    },
                )
            return result.get("post_id") or result.get("id", "")
        except PublishingError as exc:
            errors.append(f"{media_url} -> {exc}")
    raise PublishingError("Facebook publish failed for all tested media URLs: " + " | ".join(errors))


def _publish_to_instagram(target: PublishingTarget, file_obj: dict) -> str:
    if not target.instagram_account:
        return ""
    page_token = target.facebook_account.access_token if target.facebook_account else ""
    token = target.instagram_account.access_token or page_token or target.credential.access_token
    if not token:
        raise PublishingError("Instagram publishing token not available.")
    caption = build_caption(target) or file_obj["name"]
    errors = []
    mime_type = file_obj.get("mimeType", "")
    variant = "instagram_image" if mime_type.startswith("image/") else ""
    media_urls = get_cached_public_urls(target, file_obj, variant=variant or "default")
    if not media_urls:
        media_urls = build_proxy_urls(target.id, file_obj["id"], file_obj.get("name", "media"), variant=variant) if is_public_base_ready() else []
    if not media_urls:
        media_urls = get_public_media_urls(file_obj)
    for media_url in media_urls:
        try:
            payload = {"caption": caption}
            if mime_type.startswith("video/"):
                payload.update(
                    {
                        "media_type": "REELS",
                        "video_url": media_url,
                        "share_to_feed": "true",
                    }
                )
            else:
                payload["image_url"] = media_url

            creation = _graph_post(f"/{target.instagram_account.external_id}/media", token, payload)
            container_id = creation.get("id", "")
            if mime_type.startswith("video/"):
                _wait_for_instagram_container(container_id, token)
            publish = _graph_post(
                f"/{target.instagram_account.external_id}/media_publish",
                token,
                {"creation_id": container_id},
            )
            return publish.get("id") or container_id
        except PublishingError as exc:
            errors.append(f"{media_url} -> {exc}")
    raise PublishingError("Instagram publish failed for all tested public URLs: " + " | ".join(errors))


def _wait_for_instagram_container(container_id: str, access_token: str) -> None:
    if not container_id:
        raise PublishingError("Instagram container ID missing.")

    for _ in range(settings.INSTAGRAM_CONTAINER_MAX_POLLS):
        container = _graph_get(f"/{container_id}", access_token, {"fields": "status_code,status"})
        status_code = (container.get("status_code") or container.get("status") or "").upper()
        if status_code in {"FINISHED", "PUBLISHED"}:
            return
        if status_code in {"ERROR", "EXPIRED"}:
            raise PublishingError(f"Instagram container failed with status {status_code}.")
        time.sleep(settings.INSTAGRAM_CONTAINER_POLL_SECONDS)

    raise PublishingError("Instagram container processing timed out before reaching FINISHED.")


def publish_platform(target: PublishingTarget, platform: str, scheduled_for=None, file_obj: dict | None = None) -> None:
    scheduled_for = scheduled_for or timezone.now()
    file_obj = file_obj or _get_slot_locked_file(target, scheduled_for)
    if _platform_already_succeeded_for_file(target, platform, file_obj["id"]):
        return
    try:
        ensure_public_file(get_drive_service(), file_obj["id"])
    except Exception:
        pass

    log = PostLog.objects.create(
        target=target,
        platform=platform,
        scheduled_for=scheduled_for,
        drive_file_id=file_obj["id"],
        drive_file_name=file_obj["name"],
    )
    try:
        creation_id = _publish_to_facebook(target, file_obj) if platform == SocialAccount.FACEBOOK else _publish_to_instagram(target, file_obj)
        log.status = PostLog.STATUS_SUCCESS
        log.published_at = timezone.now()
        log.meta_creation_id = creation_id
        log.message = f"{platform.title()} post published."
        log.save()
    except Exception as exc:
        log.status = PostLog.STATUS_FAILED
        log.message = build_rejection_diagnostics(platform, file_obj, str(exc))
        log.save()
        raise

def publish_target(target: PublishingTarget, scheduled_for=None) -> None:
    scheduled_for = scheduled_for or timezone.now()
    failures = []
    attempted = 0
    file_obj = _get_slot_locked_file(target, scheduled_for)
    for platform in _active_platforms(target):
        if _platform_already_succeeded_for_file(target, platform, file_obj["id"]):
            continue
        try:
            publish_platform(target, platform, scheduled_for=scheduled_for, file_obj=file_obj)
            attempted += 1
        except Exception as exc:
            failures.append(f"{platform}: {exc}")

    if failures:
        target.last_status = "failed"
        target.last_error = " | ".join(failures)
        target.save(update_fields=["last_status", "last_error", "updated_at"])
        raise PublishingError(target.last_error)

    if attempted == 0:
        return

    target.last_posted_at = timezone.now()
    target.last_status = "success"
    target.last_error = ""
    target.save(update_fields=["last_posted_at", "last_status", "last_error", "updated_at"])


def publish_target_now(target: PublishingTarget) -> None:
    if not target.is_active:
        raise PublishingError("Target is inactive.")
    if not target.drive_folder_id:
        raise PublishingError("Drive folder is not configured for this target.")
    publish_target(target, scheduled_for=timezone.now())


def publish_due_targets(reference_time=None) -> dict:
    now = timezone.localtime(reference_time or timezone.now())
    catchup_window = timedelta(minutes=settings.SCHEDULER_CATCHUP_MINUTES)
    success = 0
    failed = 0
    targets = PublishingTarget.objects.filter(is_active=True).select_related("credential", "facebook_account", "instagram_account")
    for target in targets:
        try:
            if not target.drive_folder_id:
                continue
            due_slots = [
                slot
                for slot in get_daily_slots(target, now)
                if slot <= now and (now - slot) <= catchup_window
            ]
            if not due_slots:
                continue
            active_platforms = set(_active_platforms(target))
            completed_runs = 0
            for slot in due_slots:
                if _slot_is_complete(target, slot, active_platforms):
                    completed_runs += 1
                else:
                    break
            published_any = False
            if completed_runs < len(due_slots):
                publish_target(target, scheduled_for=due_slots[completed_runs])
                published_any = True
            if published_any:
                success += 1
        except (PublishingError, DriveConfigError, requests.RequestException) as exc:
            target.last_status = "failed"
            target.last_error = str(exc)
            target.save(update_fields=["last_status", "last_error", "updated_at"])
            failed += 1
    return {"success": success, "failed": failed, "checked_at": now}
