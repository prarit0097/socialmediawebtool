"""
ASGI config for social_poster project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.1/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

from social_poster.runtime_warnings import suppress_known_runtime_warnings

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'social_poster.settings')
suppress_known_runtime_warnings()

application = get_asgi_application()
