from __future__ import annotations

from urllib.parse import quote, urlencode, urljoin

from django.conf import settings
from django.core import signing
from django.urls import reverse


PROXY_SALT = "scheduler.media.proxy"


def is_public_base_ready() -> bool:
    base = settings.PUBLIC_APP_BASE_URL.strip()
    return bool(base and "localhost" not in base and "127.0.0.1" not in base)


def sign_media_token(target_id: int, file_id: str) -> str:
    return signing.dumps({"target_id": target_id, "file_id": file_id}, salt=PROXY_SALT)


def unsign_media_token(token: str, max_age: int = 60 * 60 * 24) -> dict:
    return signing.loads(token, salt=PROXY_SALT, max_age=max_age)


def build_proxy_urls(target_id: int, file_id: str, filename: str, variant: str = "") -> list[str]:
    if not is_public_base_ready():
        return []
    token = sign_media_token(target_id, file_id)
    safe_filename = quote(filename or "media")
    path = reverse("scheduler:media_proxy", kwargs={"token": token, "filename": safe_filename})
    base = settings.PUBLIC_APP_BASE_URL.rstrip("/") + "/"
    proxy_url = urljoin(base, path.lstrip("/"))
    if variant:
        query = urlencode({"variant": variant})
        proxy_url = f"{proxy_url}?{query}"
    separator = "&" if "?" in proxy_url else "?"
    return [proxy_url, f"{proxy_url}{separator}download=1"]
