from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from scheduler.models import MediaAsset, PublishingTarget
from scheduler.services.drive import download_drive_file, get_drive_file_metadata
from scheduler.services.media_transform import build_instagram_ready_image
from scheduler.services.proxy import is_public_base_ready


def _cache_dir() -> Path:
    path = Path(settings.MEDIA_CACHE_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_filename(name: str, fallback_ext: str = "") -> str:
    name = (name or "media").replace("/", "_").replace("\\", "_")
    if fallback_ext and "." not in name:
        name += fallback_ext
    return name


def build_public_asset_url(asset: MediaAsset) -> str:
    base = settings.PUBLIC_APP_BASE_URL.rstrip("/") + "/"
    path = reverse("scheduler:public_media", kwargs={"public_key": str(asset.public_key), "filename": asset.public_filename})
    return base + path.lstrip("/")


def ensure_cached_asset(target: PublishingTarget, file_obj: dict, variant: str = "default") -> MediaAsset:
    metadata = get_drive_file_metadata(file_obj["id"])
    asset, _ = MediaAsset.objects.get_or_create(
        target=target,
        drive_file_id=file_obj["id"],
        variant=variant,
        defaults={
            "drive_file_name": metadata.get("name", file_obj.get("name", "media")),
            "public_filename": metadata.get("name", file_obj.get("name", "media")),
            "source_mime_type": metadata.get("mimeType", file_obj.get("mimeType", "")),
        },
    )

    cache_root = _cache_dir()
    raw_bytes = download_drive_file(file_obj["id"])
    source_mime = metadata.get("mimeType", file_obj.get("mimeType", "application/octet-stream"))
    content_type = source_mime
    public_filename = _safe_filename(metadata.get("name", file_obj.get("name", "media")))

    if variant == "instagram_image" and source_mime.startswith("image/"):
        raw_bytes = build_instagram_ready_image(raw_bytes)
        content_type = "image/jpeg"
        stem = Path(public_filename).stem
        public_filename = f"{stem}.jpg"

    local_path = cache_root / str(asset.public_key)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(raw_bytes)

    asset.drive_file_name = metadata.get("name", file_obj.get("name", "media"))
    asset.public_filename = public_filename
    asset.local_path = str(local_path)
    asset.source_mime_type = source_mime
    asset.content_type = content_type
    asset.file_size = len(raw_bytes)
    asset.status = MediaAsset.STATUS_READY
    asset.last_error = ""
    asset.last_synced_at = timezone.now()
    asset.save()
    return asset


def get_cached_public_urls(target: PublishingTarget, file_obj: dict, variant: str = "default") -> list[str]:
    if not is_public_base_ready():
        return []
    asset = ensure_cached_asset(target, file_obj, variant=variant)
    return [build_public_asset_url(asset)]
