from __future__ import annotations

from scheduler.services.drive import get_drive_file_metadata


def describe_media_file(file_obj: dict) -> str:
    name = file_obj.get("name", "unknown")
    mime_type = file_obj.get("mimeType", "unknown")
    size = file_obj.get("size")
    if size is None and file_obj.get("id"):
        try:
            size = get_drive_file_metadata(file_obj["id"]).get("size")
        except Exception:
            size = None
    size_part = f", size={size} bytes" if size else ""
    return f"file={name}, mime={mime_type}{size_part}"


def probable_rejection_reasons(platform: str, file_obj: dict, raw_error: str) -> list[str]:
    mime_type = file_obj.get("mimeType", "")
    name = file_obj.get("name", "")
    error = (raw_error or "").lower()
    reasons = []

    if platform == "instagram":
        if mime_type.startswith("image/"):
            reasons.append("Instagram needs a publicly fetchable image URL and usually works best with JPEG plus proper image headers.")
        if mime_type.startswith("video/"):
            reasons.append("Instagram video/reels require a public MP4/MOV URL and strict codec/container specs such as H264/HEVC video and AAC audio.")
        if "only photo or video can be accepted as media type" in error:
            reasons.append("Meta could not recognize the fetched URL as a valid image/video file.")
        if "container failed with status error" in error:
            reasons.append("Meta accepted the request but rejected the media during processing, usually due to codec, bitrate, duration, or file structure.")
    if platform == "facebook":
        if "unable to fetch video file from url" in error:
            reasons.append("Facebook could not download the video from the supplied public URL.")
        if "invalid parameter" in error:
            reasons.append("The selected file or endpoint parameters did not match what Facebook expected.")

    if name.lower().endswith(".png") and platform == "instagram":
        reasons.append("If Instagram image posting still fails, convert the image to JPEG and retry.")

    return reasons


def build_rejection_diagnostics(platform: str, file_obj: dict, raw_error: str) -> str:
    summary = f"{raw_error} | Diagnostics: {describe_media_file(file_obj)}"
    reasons = probable_rejection_reasons(platform, file_obj, raw_error)
    if reasons:
        summary += " | Possible causes: " + " ; ".join(reasons)
    return summary
