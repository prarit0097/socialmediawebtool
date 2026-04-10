from __future__ import annotations

import base64
import binascii
import secrets
from functools import wraps

from django.conf import settings
from django.http import HttpResponse


def app_admin_is_configured() -> bool:
    return bool(settings.APP_ADMIN_USERNAME and settings.APP_ADMIN_PASSWORD)


def _unauthorized_response() -> HttpResponse:
    response = HttpResponse("Admin authentication required.", status=401)
    response["WWW-Authenticate"] = f'Basic realm="{settings.APP_ADMIN_REALM}"'
    return response


def _decode_basic_auth(header_value: str) -> tuple[str, str] | None:
    if not header_value.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header_value.split(" ", 1)[1].strip()).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
    if ":" not in raw:
        return None
    username, password = raw.split(":", 1)
    return username, password


def app_admin_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not app_admin_is_configured():
            return view_func(request, *args, **kwargs)

        credentials = _decode_basic_auth(request.META.get("HTTP_AUTHORIZATION", ""))
        if credentials:
            username, password = credentials
            if secrets.compare_digest(username, settings.APP_ADMIN_USERNAME) and secrets.compare_digest(
                password,
                settings.APP_ADMIN_PASSWORD,
            ):
                return view_func(request, *args, **kwargs)

        return _unauthorized_response()

    return _wrapped
