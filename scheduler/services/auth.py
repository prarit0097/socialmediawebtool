# DEPRECATED: replaced by Django session auth (django.contrib.auth). Kept as import-safe no-ops.
from __future__ import annotations

from functools import wraps

from django.shortcuts import redirect


def app_admin_is_configured() -> bool:
    """Deprecated. App-admin basic auth has been replaced by Django session auth."""
    return False


def app_admin_required(view_func):
    """Deprecated pass-through decorator. Returns the view unchanged (no auth logic)."""

    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        return view_func(request, *args, **kwargs)

    return _wrapped


def logout_response():
    """Deprecated. Kept for backward compatibility; new code uses Django logout."""
    return redirect("/")
