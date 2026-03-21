from __future__ import annotations

from io import BytesIO

from PIL import Image


def build_instagram_ready_image(raw_bytes: bytes) -> bytes:
    image = Image.open(BytesIO(raw_bytes))
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    elif image.mode == "L":
        image = image.convert("RGB")

    quality = 92
    while quality >= 70:
        output = BytesIO()
        image.save(output, format="JPEG", quality=quality, optimize=True)
        data = output.getvalue()
        if len(data) <= 8 * 1024 * 1024:
            return data
        quality -= 5
    return data
