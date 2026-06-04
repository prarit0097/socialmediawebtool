from django.conf import settings


def app_settings(request):
    return {
        "app_settings": {
            "google_service_account_email": settings.GOOGLE_SERVICE_ACCOUNT_EMAIL,
        }
    }
