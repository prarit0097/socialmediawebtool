from django.urls import path

from . import views

app_name = "scheduler"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("targets/<int:pk>/", views.target_detail, name="target_detail"),
    path("media-proxy/<str:token>/<path:filename>/", views.media_proxy, name="media_proxy"),
    path("public-media/<uuid:public_key>/<path:filename>/", views.public_media, name="public_media"),
]
