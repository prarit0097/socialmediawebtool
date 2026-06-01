from __future__ import annotations

import base64
import binascii
import secrets
from functools import wraps

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import redirect


LOGGED_OUT_COOKIE = "socialposter_logged_out"


def app_admin_is_configured() -> bool:
    return bool(settings.APP_ADMIN_USERNAME and settings.APP_ADMIN_PASSWORD)


def _unauthorized_response() -> HttpResponse:
    response = HttpResponse("Admin authentication required.", status=401)
    response["WWW-Authenticate"] = f'Basic realm="{settings.APP_ADMIN_REALM}"'
    return response


def logout_response() -> HttpResponse:
    response = redirect("/")
    response.set_cookie(LOGGED_OUT_COOKIE, "1", httponly=True, samesite="Lax")
    return response


def _logged_out_response() -> HttpResponse:
    response = HttpResponse(
        """
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Logged out</title>
            <style>
                body { margin: 0; min-height: 100vh; display: grid; place-items: center; font-family: Georgia, "Times New Roman", serif; background: #f5f0e8; color: #182126; }
                main { width: min(440px, calc(100vw - 32px)); padding: 28px; border: 1px solid #d9c8b3; border-radius: 20px; background: #fffaf3; box-shadow: 0 12px 24px rgba(24, 33, 38, 0.08); }
                h1 { margin: 0 0 10px; font-size: 2rem; }
                p { margin: 0 0 18px; color: #6f756d; }
                a { display: inline-flex; border-radius: 999px; padding: 10px 16px; border: 1px solid #2f6c63; color: #2f6c63; text-decoration: none; }
            </style>
        </head>
        <body>
            <main>
                <h1>Logged out</h1>
                <p>Admin session is locked on this browser.</p>
                <a href="/?login=1">Sign in again</a>
            </main>
        </body>
        </html>
        """,
        status=200,
    )
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
                if request.COOKIES.get(LOGGED_OUT_COOKIE) and request.GET.get("login") != "1":
                    return _logged_out_response()
                response = view_func(request, *args, **kwargs)
                if request.GET.get("login") == "1":
                    response.delete_cookie(LOGGED_OUT_COOKIE)
                return response

        return _unauthorized_response()

    return _wrapped
