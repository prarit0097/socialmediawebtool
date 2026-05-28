from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from scheduler.models import PostLog, PublishingTarget, ScheduledPostRun
from scheduler.services.compliance import SUPPORTED_INSTAGRAM_VIDEO_TYPES, build_target_policy_warnings
from scheduler.services.drive import DriveConfigError, find_caption_file, is_publishable_media, list_folder_files
from scheduler.services.proxy import is_public_base_ready


def _cache_key(target: PublishingTarget) -> str:
    latest_log_id = target.post_logs.order_by("-created_at").values_list("id", flat=True).first() or 0
    latest_run_updated = target.scheduled_runs.order_by("-updated_at").values_list("updated_at", flat=True).first()
    latest_asset_updated = target.media_assets.order_by("-updated_at").values_list("updated_at", flat=True).first()
    latest_run_stamp = latest_run_updated.isoformat() if latest_run_updated else "0"
    latest_asset_stamp = latest_asset_updated.isoformat() if latest_asset_updated else "0"
    target_stamp = target.updated_at.isoformat()
    return f"target-health:{target.pk}:{target_stamp}:{latest_log_id}:{latest_run_stamp}:{latest_asset_stamp}"


def _caption_matches_filename(caption: str, file_name: str) -> bool:
    normalized_caption = " ".join((caption or "").strip().lower().split())
    normalized_stem = " ".join(Path(file_name or "").stem.strip().lower().split())
    return bool(normalized_caption and normalized_stem and normalized_caption == normalized_stem)


def _health_backlog_start(now) -> datetime:
    local_now = timezone.localtime(now)
    backlog_days = max(int(getattr(settings, "SCHEDULER_BACKLOG_DAYS", 2)), 1)
    start_date = local_now.date() - timedelta(days=backlog_days - 1)
    return timezone.make_aware(datetime.combine(start_date, datetime.min.time()))


def _current_run(target: PublishingTarget, now=None):
    now = timezone.localtime(now or timezone.now())
    stale_lock_before = now - timedelta(minutes=30)
    return (
        target.scheduled_runs.filter(scheduled_for__lte=now, scheduled_for__gte=_health_backlog_start(now))
        .filter(
            Q(
                status__in=[
                    ScheduledPostRun.STATUS_PENDING,
                    ScheduledPostRun.STATUS_BACKOFF,
                    ScheduledPostRun.STATUS_PARTIAL_SUCCESS,
                ]
            )
            | Q(status=ScheduledPostRun.STATUS_RUNNING, locked_at__lt=stale_lock_before)
        )
        .filter(Q(next_retry_at__isnull=True) | Q(next_retry_at__lte=now))
        .exclude(drive_file_id="")
        .order_by("scheduled_for", "updated_at")
        .first()
    )


def _run_file(run: ScheduledPostRun) -> dict:
    return {
        "id": run.drive_file_id,
        "name": run.drive_file_name or "unknown",
        "mimeType": run.drive_mime_type or "unknown",
    }


def _serialize_run(run: ScheduledPostRun | None) -> dict | None:
    if not run:
        return None
    return {
        "id": run.id,
        "scheduled_for": run.scheduled_for,
        "status": run.status,
        "platform_status": dict(run.platform_status or {}),
        "drive_file_id": run.drive_file_id,
        "drive_file_name": run.drive_file_name,
        "drive_mime_type": run.drive_mime_type,
        "next_retry_at": run.next_retry_at,
        "attempt_count": run.attempt_count,
        "lock_owner": run.lock_owner,
        "locked_at": run.locked_at,
        "last_error": run.last_error,
    }


def build_target_health(target: PublishingTarget) -> dict:
    cache_key = _cache_key(target)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    issues = []
    file_count = 0
    media_count = 0
    caption_found = False
    media_files = []
    current_file = None
    current_file_source = ""
    current_run = None
    pending_platforms = []
    backoff_messages = []
    content_exhausted = False

    if not target.drive_folder_id:
        issues.append("Drive folder not configured.")
    else:
        try:
            files = list_folder_files(target.drive_folder_id)
            file_count = len(files)
            media_files = [file_obj for file_obj in files if is_publishable_media(file_obj)]
            media_count = len(media_files)
            caption_found = find_caption_file(files) is not None
            if media_count == 0:
                issues.append("No image/video files found in the Drive folder.")
            if target.instagram_account:
                unsupported_videos = [
                    file_obj.get("name", "unknown")
                    for file_obj in media_files
                    if file_obj.get("mimeType", "").startswith("video/")
                    and file_obj.get("mimeType", "").lower() not in SUPPORTED_INSTAGRAM_VIDEO_TYPES
                ]
                if unsupported_videos:
                    issues.append(
                        "Instagram video readiness warning: unsupported video type found. "
                        f"Use MP4/MOV for better Meta acceptance. Example: {unsupported_videos[0]}"
                    )
                if target.default_caption.strip():
                    filename_caption_matches = [
                        file_obj.get("name", "unknown")
                        for file_obj in media_files[:20]
                        if _caption_matches_filename(target.default_caption, file_obj.get("name", ""))
                    ]
                    if filename_caption_matches:
                        issues.append(
                            "Caption readiness warning: default caption matches a media filename. "
                            "Use human-written copy for better quality signals."
                        )
            active_platforms = []
            if target.facebook_account_id:
                active_platforms.append("facebook")
            if target.instagram_account_id:
                active_platforms.append("instagram")
            success_rows = list(
                target.post_logs.filter(status=PostLog.STATUS_SUCCESS)
                .exclude(drive_file_id="")
                .values("drive_file_id", "platform")
            )
            success_map = {}
            for row in success_rows:
                success_map.setdefault(row["drive_file_id"], set()).add(row["platform"])
            current_run = _current_run(target)
            if current_run:
                current_file = _run_file(current_run)
                current_file_source = "scheduled_run"
                status_map = dict(current_run.platform_status or {})
                succeeded = {platform for platform, status in status_map.items() if status == PostLog.STATUS_SUCCESS}
                succeeded.update(success_map.get(current_run.drive_file_id, set()))
                pending_platforms = sorted(set(active_platforms) - succeeded)
                if not pending_platforms:
                    current_run = None
                    current_file = None
                    current_file_source = ""
            if current_run is None:
                for file_obj in media_files:
                    succeeded = success_map.get(file_obj["id"], set())
                    if set(active_platforms) and succeeded != set(active_platforms):
                        current_file = file_obj
                        current_file_source = "drive_scan"
                        pending_platforms = sorted(set(active_platforms) - succeeded)
                        break
            if media_files and active_platforms and current_file is None:
                content_exhausted = True
                issues.append("All unique media files have already succeeded on every active platform. Add new files to continue posting.")
            if current_file and pending_platforms:
                from scheduler.services.publishing import recent_backoff_message, recent_credential_backoff_message

                for platform in pending_platforms:
                    message = recent_backoff_message(target, platform, current_file["id"])
                    if not message:
                        message = recent_credential_backoff_message(target, platform)
                    if message:
                        backoff_messages.append(f"{platform}: {message}")
                if backoff_messages:
                    issues.append("Backoff active: " + " | ".join(backoff_messages))
        except DriveConfigError as exc:
            issues.append(str(exc))
        except Exception as exc:
            issues.append(f"Drive check failed: {exc}")

    if not target.facebook_account and not target.instagram_account:
        issues.append("No Facebook or Instagram account linked.")

    if target.instagram_account and not caption_found and not target.default_caption.strip():
        issues.append("No caption configured (caption.txt or default caption missing). Posts will be published without a caption, which hurts engagement.")

    if (target.facebook_account or target.instagram_account) and not is_public_base_ready():
        issues.append("PUBLIC_APP_BASE_URL is missing or local-only. Instagram and cached-media publishing need a public HTTPS app URL.")

    issues.extend(build_target_policy_warnings(target))

    latest_logs = list(target.post_logs.order_by("-created_at").values("platform", "status", "message", "drive_file_name")[:5])
    latest_success = (
        target.post_logs.filter(status=PostLog.STATUS_SUCCESS, published_at__isnull=False)
        .order_by("-published_at")
        .values("platform", "drive_file_name", "published_at")
        .first()
    )
    latest_failure = (
        target.post_logs.filter(status=PostLog.STATUS_FAILED)
        .order_by("-created_at")
        .values("platform", "drive_file_name", "message", "created_at")
        .first()
    )
    due_slots = []
    next_upcoming_slot = None
    try:
        from scheduler.services.publishing import _active_platforms, _slot_is_complete, get_daily_slots

        now = timezone.localtime()
        active_platform_set = set(_active_platforms(target))
        for slot in get_daily_slots(target, now):
            run = target.scheduled_runs.filter(scheduled_for=slot).first()
            if run:
                slot_status = run.status
            elif _slot_is_complete(target, slot, active_platform_set):
                slot_status = ScheduledPostRun.STATUS_SUCCESS
            elif slot <= now:
                slot_status = "due"
            else:
                slot_status = "upcoming"
                if next_upcoming_slot is None:
                    next_upcoming_slot = slot
            due_slots.append({"slot": slot, "status": slot_status})
    except Exception as exc:
        issues.append(f"Schedule status unavailable: {exc}")
        due_slots = []

    overall = "ready" if not issues else "warning"
    if any(log["status"] == PostLog.STATUS_FAILED for log in latest_logs):
        overall = "warning"

    health = {
        "overall": overall,
        "issues": issues,
        "file_count": file_count,
        "media_count": media_count,
        "caption_found": caption_found,
        "cached_asset_count": getattr(target, "ready_media_asset_count", target.media_assets.filter(status="ready").count()),
        "latest_logs": latest_logs,
        "latest_success": latest_success,
        "latest_failure": latest_failure,
        "active_platforms": [platform for platform in ("facebook", "instagram") if getattr(target, f"{platform}_account_id", None)],
        "current_file": current_file,
        "current_file_source": current_file_source,
        "current_run": _serialize_run(current_run),
        "pending_platforms": pending_platforms,
        "backoff_messages": backoff_messages,
        "content_exhausted": content_exhausted,
        "public_base_ready": is_public_base_ready(),
        "due_slots": due_slots,
        "next_upcoming_slot": next_upcoming_slot,
    }
    cache.set(cache_key, health, settings.HEALTH_CACHE_TTL_SECONDS)
    return health
