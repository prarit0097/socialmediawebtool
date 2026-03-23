from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageOps


INSTAGRAM_IMAGE_MAX_BYTES = 4 * 1024 * 1024
INSTAGRAM_IMAGE_MAX_DIMENSION = 1440


def build_instagram_ready_image(raw_bytes: bytes) -> bytes:
    image = Image.open(BytesIO(raw_bytes))
    image = ImageOps.exif_transpose(image)
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    elif image.mode == "L":
        image = image.convert("RGB")

    if max(image.size) > INSTAGRAM_IMAGE_MAX_DIMENSION:
        image.thumbnail((INSTAGRAM_IMAGE_MAX_DIMENSION, INSTAGRAM_IMAGE_MAX_DIMENSION), Image.Resampling.LANCZOS)

    quality = 92
    while quality >= 70:
        output = BytesIO()
        image.save(
            output,
            format="JPEG",
            quality=quality,
            optimize=True,
            progressive=False,
            subsampling="4:2:0",
            icc_profile=None,
            exif=b"",
        )
        data = output.getvalue()
        if len(data) <= INSTAGRAM_IMAGE_MAX_BYTES:
            return data
        quality -= 5
    return data
