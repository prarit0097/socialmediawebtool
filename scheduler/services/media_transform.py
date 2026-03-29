from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageOps


# Instagram allows up to 8 MB for images; previous 4 MB cap was unnecessarily tight
INSTAGRAM_IMAGE_MAX_BYTES = 8 * 1024 * 1024
INSTAGRAM_IMAGE_MAX_DIMENSION = 1440
# Instagram accepted aspect-ratio range: 4:5 portrait to 1.91:1 landscape
INSTAGRAM_MIN_ASPECT = 0.8
INSTAGRAM_MAX_ASPECT = 1.91


def _enforce_instagram_aspect_ratio(image: Image.Image) -> Image.Image:
    """Pad image with white bars to fit within Instagram's accepted ratio range."""
    w, h = image.size
    if h == 0:
        return image
    aspect = w / h

    if INSTAGRAM_MIN_ASPECT <= aspect <= INSTAGRAM_MAX_ASPECT:
        return image

    if aspect < INSTAGRAM_MIN_ASPECT:
        # Too tall / narrow -> pad width to reach 4:5
        new_w = int(h * INSTAGRAM_MIN_ASPECT)
        padded = Image.new("RGB", (new_w, h), (255, 255, 255))
        padded.paste(image, ((new_w - w) // 2, 0))
        return padded

    # Too wide -> pad height to reach 1.91:1
    new_h = int(w / INSTAGRAM_MAX_ASPECT)
    padded = Image.new("RGB", (w, new_h), (255, 255, 255))
    padded.paste(image, (0, (new_h - h) // 2))
    return padded


def build_instagram_ready_image(raw_bytes: bytes) -> bytes:
    image = Image.open(BytesIO(raw_bytes))
    image = ImageOps.exif_transpose(image)

    # Extract ICC profile BEFORE convert() — .convert() creates a new Image
    # that does not carry over .info, so reading it afterwards loses the profile.
    original_icc = image.info.get("icc_profile", b"")

    if image.mode != "RGB":
        image = image.convert("RGB")

    image = _enforce_instagram_aspect_ratio(image)

    if max(image.size) > INSTAGRAM_IMAGE_MAX_DIMENSION:
        image.thumbnail(
            (INSTAGRAM_IMAGE_MAX_DIMENSION, INSTAGRAM_IMAGE_MAX_DIMENSION),
            Image.Resampling.LANCZOS,
        )

    quality = 95
    while quality >= 75:
        output = BytesIO()
        save_kwargs = {
            "format": "JPEG",
            "quality": quality,
            "optimize": True,
            "progressive": False,
            # 4:4:4 keeps full chroma resolution (4:2:0 was losing colour detail)
            "subsampling": "4:4:4",
            "exif": b"",
        }
        if original_icc:
            save_kwargs["icc_profile"] = original_icc
        image.save(output, **save_kwargs)
        data = output.getvalue()
        if len(data) <= INSTAGRAM_IMAGE_MAX_BYTES:
            return data
        quality -= 5
    return data
