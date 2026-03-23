"""
WSGI config for social_poster project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.1/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

from social_poster.runtime_warnings import suppress_known_runtime_warnings

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'social_poster.settings')
suppress_known_runtime_warnings()

application = get_wsgi_application()
