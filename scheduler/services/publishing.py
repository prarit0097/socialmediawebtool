from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import time

import requests
from django.conf import settings
from django.utils import timezone

from scheduler.models import PostLog, PublishingTarget, SocialAccount
from scheduler.services.ai import AIServiceError, build_ai_caption_for_media
from scheduler.services.cache import build_public_asset_url, ensure_cached_asset, get_cached_public_urls
from scheduler.services.compliance import evaluate_publish_readiness
from scheduler.services.diagnostics import build_rejection_diagnostics
from scheduler.services.drive import (
    DriveConfigError,
    download_drive_file,
    find_caption_file,
    get_public_media_urls,
    is_publishable_media,
    list_folder_files,
)
from scheduler.services.proxy import build_proxy_urls, is_public_base_ready


class PublishingError(Exception):
    pass


class PublishBackoff(PublishingError):
    pass


RATE_LIMIT_MARKERS = (
    "application request limit reached",
    "rate limit",
    "too many calls",
    "calls to this api have exceeded",
)
TRANSIENT_BACKOFF_MARKERS = (
    "authorization error",
    "media id is not available",
    "not available",
    "not ready",
    "not finished",
    "processing",
    "processing timed out",
    "meta request failed after retries",
    "binary upload failed",
    "timeout",
    "temporarily unavailable",
    "instagram status polling failed after container creation",
)
CONTENT_EXHAUSTED_MARKERS = (
    "all unique media files",
    "no publishable image or video files",
)


def _parse_graph_response(response) -> dict:
    try:
        data = response.json()
    except ValueError:
        snippet = (response.text or "").strip()
        if snippet:
            snippet = snippet[:300]
        else:
            snippet = "<empty response body>"
        raise PublishingError(
            f"Meta returned a non-JSON response (status {response.status_code}): {snippet}"
        )
    return data


def _is_meta_rate_limit_error(message: str) -> bool:
    text = (message or "").lower()
    return any(marker in text for marker in RATE_LIMIT_MARKERS)


def _is_transient_backoff_error(message: str) -> bool:
    text = (message or "").lower()
    return any(marker in text for marker in TRANSIENT_BACKOFF_MARKERS)


def _is_content_exhausted_error(message: str) -> bool:
    text = (message or "").lower()
    return any(marker in text for marker in CONTENT_EXHAUSTED_MARKERS)


def _app_rate_limit_backoff_minutes() -> int:
    return getattr(
        settings,
        "META_APP_RATE_LIMIT_BACKOFF_MINUTES",
        settings.META_RATE_LIMIT_BACKOFF_MINUTES,
    )


def _backoff_minutes_for_failure(message: str) -> int:
    if _is_meta_rate_limit_error(message):
        return _app_rate_limit_backoff_minutes()
    if _is_transient_backoff_error(message):
        return settings.META_RATE_LIMIT_BACKOFF_MINUTES
    return 0


def _format_graph_error(data: dict, fallback_text: str = "") -> str:
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return fallback_text

    parts = [str(error.get("message") or fallback_text or "Meta Graph API error").strip()]
    detail_parts = []
    for label, key in (
        ("type", "type"),
        ("code", "code"),
        ("subcode", "error_subcode"),
        ("trace", "fbtrace_id"),
    ):
        value = error.get(key)
        if value not in (None, ""):
            detail_parts.append(f"{label}={value}")
    if detail_parts:
        parts.append(f"Meta error details: {', '.join(detail_parts)}")
    return " | ".join(part for part in parts if part)


def _backoff_message_for_failure(message: str, scope: str = "target/platform/file") -> str:
    if _is_meta_rate_limit_error(message):
        return (
            f"Meta rate limit backoff active for this {scope}. "
            f"Skipping publish retry for up to {_app_rate_limit_backoff_minutes()} minutes after the latest rate-limit response."
        )
    if _is_transient_backoff_error(message):
        return (
            f"Meta transient backoff active for this {scope}. "
            f"Skipping publish retry for up to {settings.META_RATE_LIMIT_BACKOFF_MINUTES} minutes after the latest transient Meta failure."
        )
    return ""


def recent_backoff_message(target: PublishingTarget, platform: str, drive_file_id: str) -> str:
    if not drive_file_id:
        return ""
    now = timezone.now()
    since = now - timedelta(minutes=max(settings.META_RATE_LIMIT_BACKOFF_MINUTES, _app_rate_limit_backoff_minutes()))
    recent_messages = (
        target.post_logs.filter(
            platform=platform,
            drive_file_id=drive_file_id,
            status=PostLog.STATUS_FAILED,
            created_at__gte=since,
        )
        .exclude(message="")
        .order_by("-created_at")
        .values_list("message", "created_at")[:10]
    )
    for message, created_at in recent_messages:
        backoff_minutes = _backoff_minutes_for_failure(message)
        if not backoff_minutes or created_at < now - timedelta(minutes=backoff_minutes):
            continue
        backoff_message = _backoff_message_for_failure(message)
        if backoff_message:
            return backoff_message
    return ""


def recent_credential_backoff_message(target: PublishingTarget, platform: str) -> str:
    if not target.credential_id:
        return ""
    now = timezone.now()
    since = now - timedelta(minutes=_app_rate_limit_backoff_minutes())
    recent_messages = (
        PostLog.objects.filter(
            target__credential=target.credential,
            platform=platform,
            status=PostLog.STATUS_FAILED,
            created_at__gte=since,
        )
        .exclude(message="")
        .order_by("-created_at")
        .values_list("message", "created_at")[:20]
    )
    for message, created_at in recent_messages:
        if not _is_meta_rate_limit_error(message):
            continue
        backoff_minutes = _backoff_minutes_for_failure(message)
        if not backoff_minutes or created_at < now - timedelta(minutes=backoff_minutes):
            continue
        return _backoff_message_for_failure(message, scope="credential/platform")
    return ""


def _request_with_retries(method: str, url: str, **kwargs):
    last_exc = None
    for attempt in range(settings.META_GRAPH_RETRY_COUNT + 1):
        try:
            return requests.request(method, url, timeout=settings.META_GRAPH_TIMEOUT_SECONDS, **kwargs)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            if attempt >= settings.META_GRAPH_RETRY_COUNT:
                break
            time.sleep(settings.META_GRAPH_RETRY_SLEEP_SECONDS)
    raise PublishingError(f"Meta request failed after retries: {last_exc}")


def _graph_post(path: str, access_token: str, payload: dict) -> dict:
    response = _request_with_retries(
        "post",
        f"{settings.META_GRAPH_BASE_URL}{path}",
        data={**payload, "access_token": access_token},
    )
    data = _parse_graph_response(response)
    if response.status_code >= 400 or data.get("error"):
        message = _format_graph_error(data, response.text)
        raise PublishingError(message)
    return data


def _graph_post_multipart(path: str, access_token: str, payload: dict,
                          file_field: str, file_data: tuple) -> dict:
    """Post to Meta Graph API with multipart file upload (binary source).

    Uses a longer timeout than URL-based calls because the full file body is
    being uploaded in the request.  Retries are skipped for binary uploads —
    re-sending a large file on transient errors wastes time and the first
    attempt already waited long enough.
    """
    upload_timeout = max(settings.META_GRAPH_TIMEOUT_SECONDS, 300)
    try:
        response = requests.post(
            f"{settings.META_GRAPH_BASE_URL}{path}",
            data={**payload, "access_token": access_token},
            files={file_field: file_data},
            timeout=upload_timeout,
        )
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise PublishingError(f"Binary upload failed: {exc}")
    data = _parse_graph_response(response)
    if response.status_code >= 400 or data.get("error"):
        message = _format_graph_error(data, response.text)
        raise PublishingError(message)
    return data


def _graph_get(path: str, access_token: str, params: dict | None = None) -> dict:
    query = {"access_token": access_token}
    if params:
        query.update(params)
    response = _request_with_retries(
        "get",
        f"{settings.META_GRAPH_BASE_URL}{path}",
        params=query,
    )
    data = _parse_graph_response(response)
    if response.status_code >= 400 or data.get("error"):
        message = _format_graph_error(data, response.text)
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


def build_caption(target: PublishingTarget, file_obj: dict | None = None) -> str:
    if file_obj and target.ai_enabled and target.ai_auto_caption_enabled:
        try:
            ai_caption = build_ai_caption_for_media(target, file_obj)
            if ai_caption:
                return ai_caption.replace("\r\n", "\n").replace("\r", "\n")
        except AIServiceError:
            pass

    if target.default_caption.strip():
        return target.default_caption.strip().replace("\r\n", "\n").replace("\r", "\n")

    files = list_folder_files(target.drive_folder_id)
    caption_file = find_caption_file(files)
    if not caption_file:
        return ""

    caption_bytes = download_drive_file(caption_file["id"])
    return caption_bytes.decode("utf-8-sig", errors="replace").strip().replace("\r\n", "\n").replace("\r", "\n")


def _publish_to_facebook(target: PublishingTarget, file_obj: dict) -> str:
    if not target.facebook_account:
        return ""
    token = target.facebook_account.access_token or target.credential.access_token
    if not token:
        raise PublishingError("Facebook page access token not available.")
    caption = build_caption(target, file_obj=file_obj)
    mime_type = file_obj.get("mimeType", "")

    # Pre-cache the asset once (avoids duplicate Drive downloads in fallback path)
    asset = None
    try:
        asset = ensure_cached_asset(target, file_obj, variant="default")
    except Exception:
        pass

    errors: list[str] = []

    # Strategy 1: Direct binary upload — matches how official apps publish
    # and typically receives better algorithmic distribution from Meta.
    if asset and asset.local_path and Path(asset.local_path).exists():
        try:
            filename = asset.public_filename or file_obj.get("name", "media")
            content_type = asset.content_type or mime_type or "application/octet-stream"
            with Path(asset.local_path).open("rb") as file_handle:
                if mime_type.startswith("video/"):
                    result = _graph_post_multipart(
                        f"/{target.facebook_account.external_id}/videos",
                        token,
                        {
                            "description": caption,
                            "published": "true",
                        },
                        "source",
                        (filename, file_handle, content_type),
                    )
                else:
                    result = _graph_post_multipart(
                        f"/{target.facebook_account.external_id}/photos",
                        token,
                        {
                            "caption": caption,
                            "published": "true",
                        },
                        "source",
                        (filename, file_handle, content_type),
                    )
            return result.get("post_id") or result.get("id", "")
        except PublishingError as exc:
            errors.append(f"binary upload -> {exc}")

    # Strategy 2: URL-based upload (fallback)
    media_urls = []
    if asset and is_public_base_ready():
        media_urls = [build_public_asset_url(asset)]
    if not media_urls and settings.ALLOW_LEGACY_PUBLIC_MEDIA_FALLBACK:
        media_urls = build_proxy_urls(target.id, file_obj["id"], file_obj.get("name", "media")) if is_public_base_ready() else []
    if not media_urls and settings.ALLOW_LEGACY_PUBLIC_MEDIA_FALLBACK:
        media_urls = get_public_media_urls(file_obj)
    for media_url in media_urls:
        try:
            if mime_type.startswith("video/"):
                result = _graph_post(
                    f"/{target.facebook_account.external_id}/videos",
                    token,
                    {
                        "file_url": media_url,
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
    raise PublishingError("Facebook publish failed: " + " | ".join(errors))


def _publish_to_instagram(target: PublishingTarget, file_obj: dict) -> str:
    if not target.instagram_account:
        return ""
    page_token = target.facebook_account.access_token if target.facebook_account else ""
    token = target.instagram_account.access_token or page_token or target.credential.access_token
    if not token:
        raise PublishingError("Instagram publishing token not available.")
    caption = build_caption(target, file_obj=file_obj)
    errors = []
    mime_type = file_obj.get("mimeType", "")
    variant = "instagram_image" if mime_type.startswith("image/") else ""
    media_urls = get_cached_public_urls(target, file_obj, variant=variant or "default")
    if not media_urls and settings.ALLOW_LEGACY_PUBLIC_MEDIA_FALLBACK:
        media_urls = build_proxy_urls(target.id, file_obj["id"], file_obj.get("name", "media"), variant=variant) if is_public_base_ready() else []
    if not media_urls and settings.ALLOW_LEGACY_PUBLIC_MEDIA_FALLBACK:
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
            try:
                _wait_for_instagram_container(container_id, token)
            except PublishingError as exc:
                if not _is_instagram_status_poll_auth_error(exc):
                    raise
                publish = _publish_instagram_container_with_retry(
                    target.instagram_account.external_id,
                    container_id,
                    token,
                    wait_error=exc,
                )
                return publish.get("id") or container_id
            publish = _publish_instagram_container(target.instagram_account.external_id, container_id, token)
            return publish.get("id") or container_id
        except PublishingError as exc:
            errors.append(f"{media_url} -> {exc}")
    raise PublishingError("Instagram publish failed for all tested public URLs: " + " | ".join(errors))


def _publish_instagram_container(instagram_account_id: str, container_id: str, access_token: str) -> dict:
    return _graph_post(
        f"/{instagram_account_id}/media_publish",
        access_token,
        {"creation_id": container_id},
    )


def _is_instagram_status_poll_auth_error(exc: PublishingError) -> bool:
    message = str(exc).lower()
    return "authorization" in message or "permission" in message or "oauth" in message


def _is_instagram_container_not_ready_error(exc: PublishingError) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "not available",
            "not ready",
            "not finished",
            "processing",
            "not been processed",
            "media id",
        )
    )


def _publish_instagram_container_with_retry(
    instagram_account_id: str,
    container_id: str,
    access_token: str,
    *,
    wait_error: PublishingError,
) -> dict:
    last_publish_error: PublishingError | None = None
    for _ in range(settings.INSTAGRAM_CONTAINER_MAX_POLLS):
        time.sleep(settings.INSTAGRAM_CONTAINER_POLL_SECONDS)
        try:
            return _publish_instagram_container(instagram_account_id, container_id, access_token)
        except PublishingError as exc:
            last_publish_error = exc
            if not _is_instagram_container_not_ready_error(exc):
                break
    if last_publish_error:
        raise PublishingError(
            "Instagram status polling failed after container creation, and direct publish fallback also failed. "
            f"Status poll error: {wait_error}. Publish error: {last_publish_error}"
        )
    raise wait_error


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
    backoff_message = recent_backoff_message(target, platform, file_obj["id"])
    if not backoff_message:
        backoff_message = recent_credential_backoff_message(target, platform)
    if backoff_message:
        raise PublishBackoff(backoff_message)
    caption = build_caption(target, file_obj=file_obj)
    compliance = evaluate_publish_readiness(target, platform, file_obj, caption)
    if compliance.is_blocked:
        raise PublishingError(" | ".join(compliance.blocking_issues))

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
        warning_suffix = f" Warnings: {' ; '.join(compliance.warnings)}" if compliance.warnings else ""
        log.message = f"{platform.title()} post published.{warning_suffix}"
        log.save()
    except Exception as exc:
        log.status = PostLog.STATUS_FAILED
        warning_suffix = f" | Preflight warnings: {' ; '.join(compliance.warnings)}" if compliance.warnings else ""
        log.message = build_rejection_diagnostics(platform, file_obj, str(exc)) + warning_suffix
        log.save()
        raise

def publish_target(target: PublishingTarget, scheduled_for=None) -> None:
    scheduled_for = scheduled_for or timezone.now()
    failures = []
    backoffs = []
    attempted = 0
    file_obj = _get_slot_locked_file(target, scheduled_for)
    for platform in _active_platforms(target):
        if _platform_already_succeeded_for_file(target, platform, file_obj["id"]):
            continue
        try:
            publish_platform(target, platform, scheduled_for=scheduled_for, file_obj=file_obj)
            attempted += 1
        except PublishBackoff as exc:
            backoffs.append(f"{platform}: {exc}")
        except Exception as exc:
            failures.append(f"{platform}: {exc}")

    if failures:
        target.last_status = "failed"
        target.last_error = " | ".join(failures)
        target.save(update_fields=["last_status", "last_error", "updated_at"])
        raise PublishingError(target.last_error)

    if backoffs:
        target.last_status = "backoff"
        target.last_error = " | ".join(backoffs)
        target.save(update_fields=["last_status", "last_error", "updated_at"])
        raise PublishBackoff(target.last_error)

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
    skipped = 0
    backoff = 0
    content_exhausted = 0
    checked_targets = 0
    targets = PublishingTarget.objects.filter(is_active=True).select_related("credential", "facebook_account", "instagram_account")
    for target in targets:
        checked_targets += 1
        try:
            if not target.drive_folder_id:
                skipped += 1
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
        except PublishBackoff as exc:
            target.last_status = "backoff"
            target.last_error = str(exc)
            target.save(update_fields=["last_status", "last_error", "updated_at"])
            skipped += 1
            backoff += 1
        except (PublishingError, DriveConfigError, requests.RequestException) as exc:
            if _is_content_exhausted_error(str(exc)):
                target.last_status = "content_exhausted"
                skipped += 1
                content_exhausted += 1
            else:
                target.last_status = "failed"
                failed += 1
            target.last_error = str(exc)
            target.save(update_fields=["last_status", "last_error", "updated_at"])
    return {
        "success": success,
        "failed": failed,
        "skipped": skipped,
        "backoff": backoff,
        "content_exhausted": content_exhausted,
        "checked_targets": checked_targets,
        "checked_at": now,
    }
