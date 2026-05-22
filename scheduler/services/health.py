from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.cache import cache

from scheduler.models import PostLog, PublishingTarget
from scheduler.services.compliance import SUPPORTED_INSTAGRAM_VIDEO_TYPES, build_target_policy_warnings
from scheduler.services.drive import DriveConfigError, find_caption_file, is_publishable_media, list_folder_files
from scheduler.services.proxy import is_public_base_ready


def _cache_key(target: PublishingTarget) -> str:
    return f"target-health:{target.pk}:{int(target.updated_at.timestamp())}"


def _caption_matches_filename(caption: str, file_name: str) -> bool:
    normalized_caption = " ".join((caption or "").strip().lower().split())
    normalized_stem = " ".join(Path(file_name or "").stem.strip().lower().split())
    return bool(normalized_caption and normalized_stem and normalized_caption == normalized_stem)


def build_target_health(target: PublishingTarget) -> dict:
    cache_key = _cache_key(target)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    issues = []
    file_count = 0
    media_count = 0
    caption_found = False

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
    }
    cache.set(cache_key, health, settings.HEALTH_CACHE_TTL_SECONDS)
    return health
