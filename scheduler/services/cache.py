from __future__ import annotations

from pathlib import Path
import uuid

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from scheduler.models import MediaAsset, PublishingTarget
from scheduler.services.drive import download_drive_file, download_drive_file_to_path, get_drive_file_metadata
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


def _drive_fingerprint(metadata: dict, fallback: dict) -> str:
    parts = [
        metadata.get("modifiedTime", ""),
        metadata.get("md5Checksum", ""),
        metadata.get("size", fallback.get("size", "")),
        metadata.get("mimeType", fallback.get("mimeType", "")),
        metadata.get("name", fallback.get("name", "")),
        metadata.get("id", fallback.get("id", "")),
    ]
    return "|".join(str(part or "") for part in parts)


def _has_usable_local_file(asset: MediaAsset) -> bool:
    if asset.status != MediaAsset.STATUS_READY or not asset.local_path or not asset.file_size:
        return False
    local_path = Path(asset.local_path)
    return local_path.exists() and local_path.stat().st_size == asset.file_size


def _mark_asset_failed(asset: MediaAsset, exc: Exception) -> None:
    update_fields = ["last_error", "last_synced_at", "updated_at"]
    if not _has_usable_local_file(asset):
        asset.status = MediaAsset.STATUS_FAILED
        update_fields.insert(0, "status")
    asset.last_error = str(exc)[:1000]
    asset.last_synced_at = timezone.now()
    asset.save(update_fields=update_fields)


def ensure_cached_asset(target: PublishingTarget, file_obj: dict, variant: str = "default") -> MediaAsset:
    asset, created = MediaAsset.objects.get_or_create(
        target=target,
        drive_file_id=file_obj["id"],
        variant=variant,
        defaults={
            "drive_file_name": file_obj.get("name", "media"),
            "public_filename": file_obj.get("name", "media"),
            "source_mime_type": file_obj.get("mimeType", ""),
        },
    )

    try:
        metadata = get_drive_file_metadata(file_obj["id"])
        source_fingerprint = _drive_fingerprint(metadata, file_obj)

        # Skip re-download if asset is already cached and the local file exists.
        if not created and asset.status == MediaAsset.STATUS_READY and asset.local_path:
            local_path = Path(asset.local_path)
            fingerprint_matches = bool(asset.source_fingerprint) and asset.source_fingerprint == source_fingerprint
            if (
                fingerprint_matches
                and local_path.exists()
                and asset.file_size
                and local_path.stat().st_size == asset.file_size
            ):
                return asset

        cache_root = _cache_dir()
        source_mime = metadata.get("mimeType", file_obj.get("mimeType", "application/octet-stream"))
        content_type = source_mime
        public_filename = _safe_filename(metadata.get("name", file_obj.get("name", "media")))
        local_path = cache_root / str(asset.public_key)
        temp_path = cache_root / f"{asset.public_key}.{uuid.uuid4().hex}.tmp"
        local_path.parent.mkdir(parents=True, exist_ok=True)

        if variant == "instagram_image" and source_mime.startswith("image/"):
            raw_bytes = build_instagram_ready_image(download_drive_file(file_obj["id"]))
            content_type = "image/jpeg"
            stem = Path(public_filename).stem
            public_filename = f"{stem}.jpg"
            temp_path.write_bytes(raw_bytes)
            file_size = len(raw_bytes)
        elif source_mime.startswith("video/"):
            file_size = download_drive_file_to_path(file_obj["id"], temp_path)
        else:
            raw_bytes = download_drive_file(file_obj["id"])
            temp_path.write_bytes(raw_bytes)
            file_size = len(raw_bytes)

        temp_path.replace(local_path)

        asset.drive_file_name = metadata.get("name", file_obj.get("name", "media"))
        asset.public_filename = public_filename
        asset.local_path = str(local_path)
        asset.source_mime_type = source_mime
        asset.drive_modified_time = str(metadata.get("modifiedTime", ""))
        asset.drive_checksum = str(metadata.get("md5Checksum", ""))
        asset.source_fingerprint = source_fingerprint
        asset.content_type = content_type
        asset.file_size = file_size
        asset.status = MediaAsset.STATUS_READY
        asset.last_error = ""
        asset.last_synced_at = timezone.now()
        asset.save()
        return asset
    except Exception as exc:
        if "temp_path" in locals():
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        _mark_asset_failed(asset, exc)
        raise


def get_cached_public_urls(target: PublishingTarget, file_obj: dict, variant: str = "default") -> list[str]:
    if not is_public_base_ready():
        return []
    asset = ensure_cached_asset(target, file_obj, variant=variant)
    return [build_public_asset_url(asset)]
