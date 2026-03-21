from django.contrib import admin

from .models import DailyReportLog, MediaAsset, MetaCredential, PostLog, PublishingTarget, SocialAccount


@admin.register(MetaCredential)
class MetaCredentialAdmin(admin.ModelAdmin):
    list_display = ("label", "user_name", "user_id", "is_active", "last_sync_at")
    search_fields = ("label", "user_name", "user_id")


@admin.register(SocialAccount)
class SocialAccountAdmin(admin.ModelAdmin):
    list_display = ("display_name", "platform", "credential", "external_id", "is_active")
    list_filter = ("platform", "is_active")
    search_fields = ("name", "username", "external_id")


@admin.register(PublishingTarget)
class PublishingTargetAdmin(admin.ModelAdmin):
    list_display = ("display_name", "credential", "posts_per_day", "is_active", "last_status")
    list_filter = ("is_active",)
    search_fields = ("display_name", "drive_folder_id")


@admin.register(PostLog)
class PostLogAdmin(admin.ModelAdmin):
    list_display = ("target", "platform", "scheduled_for", "status", "published_at")
    list_filter = ("platform", "status")
    search_fields = ("drive_file_name", "drive_file_id", "meta_creation_id")


@admin.register(MediaAsset)
class MediaAssetAdmin(admin.ModelAdmin):
    list_display = ("drive_file_name", "variant", "target", "status", "content_type", "file_size", "last_synced_at")
    list_filter = ("variant", "status", "content_type")
    search_fields = ("drive_file_name", "drive_file_id", "public_filename", "public_key")


@admin.register(DailyReportLog)
class DailyReportLogAdmin(admin.ModelAdmin):
    list_display = ("report_date", "status", "sent_at", "telegram_chat_id")
