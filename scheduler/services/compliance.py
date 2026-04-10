from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings

from scheduler.models import PublishingTarget, SocialAccount
from scheduler.services.proxy import is_public_base_ready


TEMP_HOST_MARKERS = (
    "ngrok-free.dev",
    "ngrok.io",
    "ngrok.app",
    "loca.lt",
    "trycloudflare.com",
)
SUPPORTED_INSTAGRAM_VIDEO_TYPES = {"video/mp4", "video/quicktime"}


@dataclass
class ComplianceResult:
    blocking_issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocking_issues)


def _public_base_host() -> str:
    public_base_url = settings.PUBLIC_APP_BASE_URL.strip()
    if not public_base_url:
        return ""
    return urlparse(public_base_url).netloc.lower()


def public_base_uses_temporary_host() -> bool:
    host = _public_base_host()
    return bool(host and any(marker in host for marker in TEMP_HOST_MARKERS))


def _normalized_caption(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def _caption_looks_like_filename(caption: str, file_obj: dict) -> bool:
    caption_text = _normalized_caption(caption)
    stem = _normalized_caption(Path(file_obj.get("name", "")).stem)
    return bool(caption_text and stem and caption_text == stem)


def build_target_policy_warnings(target: PublishingTarget) -> list[str]:
    warnings: list[str] = []
    if target.instagram_account and not is_public_base_ready():
        warnings.append("Instagram publishing needs a stable HTTPS PUBLIC_APP_BASE_URL for cached media delivery.")
    if public_base_uses_temporary_host():
        warnings.append("PUBLIC_APP_BASE_URL is using a temporary tunnel host. Production posting should use a stable HTTPS domain.")
    if target.instagram_account and not (target.instagram_account.access_token or target.facebook_account_id or target.credential.access_token):
        warnings.append("Instagram token chain looks weak. A linked Facebook page token is preferred for reliable publishing.")
    if target.facebook_account and not target.facebook_account.access_token:
        warnings.append("Facebook page access token is missing; the app may need to fall back to the broader credential token.")
    return warnings


def evaluate_publish_readiness(
    target: PublishingTarget,
    platform: str,
    file_obj: dict,
    caption: str,
) -> ComplianceResult:
    result = ComplianceResult(warnings=build_target_policy_warnings(target))
    mime_type = (file_obj.get("mimeType") or "").lower()

    if not target.is_active:
        result.blocking_issues.append("Target is inactive.")
    if not target.drive_folder_id:
        result.blocking_issues.append("Drive folder is not configured.")
    if platform == SocialAccount.FACEBOOK and not target.facebook_account_id:
        result.blocking_issues.append("Facebook account is not linked for this target.")
    if platform == SocialAccount.INSTAGRAM and not target.instagram_account_id:
        result.blocking_issues.append("Instagram account is not linked for this target.")

    if not (mime_type.startswith("image/") or mime_type.startswith("video/")):
        result.blocking_issues.append(f"Unsupported media type for publishing: {mime_type or 'unknown'}.")

    if platform == SocialAccount.INSTAGRAM:
        if not is_public_base_ready():
            result.blocking_issues.append("Instagram publishing is blocked until PUBLIC_APP_BASE_URL is a public HTTPS URL.")
        if mime_type.startswith("video/") and mime_type not in SUPPORTED_INSTAGRAM_VIDEO_TYPES:
            result.blocking_issues.append("Instagram video publishing only supports MP4 or MOV inputs in this app.")

    if platform == SocialAccount.FACEBOOK and not (target.facebook_account and (target.facebook_account.access_token or target.credential.access_token)):
        result.blocking_issues.append("Facebook publish token is not available.")
    if platform == SocialAccount.INSTAGRAM and not (
        (target.instagram_account and target.instagram_account.access_token)
        or (target.facebook_account and target.facebook_account.access_token)
        or target.credential.access_token
    ):
        result.blocking_issues.append("Instagram publish token is not available.")

    if not caption.strip():
        result.warnings.append("Caption is empty. Empty captions are allowed but usually reduce engagement.")
    elif _caption_looks_like_filename(caption, file_obj):
        result.warnings.append("Caption matches the raw filename. Replace it with human-written copy to avoid low-quality signals.")

    return result
